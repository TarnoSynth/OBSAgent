"""``read_note`` — zwraca tresc notatki z vaulta (Faza 4 refaktoru).

**Semantyka:**

Zwraca ``{frontmatter, body, wikilinks_out, wikilinks_in}`` dla jednej
notatki. Gdy ``sections=[...]`` — body ograniczone do wskazanych sekcji
(oszczedzamy tokeny na duzych hubach).

**Dlaczego in-links sa w odpowiedzi:**

In-links (``wikilinks_in``) mowia modelowi "ktore inne notatki wzmiankuja
tę". Uzyteczne zanim zaproponuje zmiane sensu notatki — zeby wiedziec,
ze ta zmiana zerwie semantyke innych notatek. Pre-komputowane w
``VaultKnowledge.backlinks_index``, zwracamy je O(1).

**Sections:**

Jesli ``sections`` podane — parsujemy body po headingach (dowolny poziom),
zwracamy tylko te, ktorych tytul matchuje dokladnie (trim+case-sensitive).
Niepasujace sekcje wymienione w ``sections`` trafiaja do pola
``missing_sections`` w ``structured`` — model widzi, czego nie znalazl.

**Pending writes:**

Gdy w tej sesji jest juz zarejestrowany ``create``/``update``/``append``
na tej sciezce, ``read_note`` zwraca **efektywny** stan (replay pending
writes), nie tylko to co jest na dysku. Dzieki temu model moze potem
zrobic ``append_section`` na dopiero co zaproponowanej notatce.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext
from src.agent.tools.vault_write._common import (
    normalize_path_or_error,
    path_exists_effectively,
)
from src.agent.tools.vault_write._granular import compute_effective_content
from src.agent.tools.vault_write._markdown_ops import (
    MarkdownOpsError,
    _find_heading_spans,
    _iter_code_fence_mask,
    _join_lines,
    _split_lines_preserving,
    parse_frontmatter,
)


class _ReadNoteArgs(BaseModel):
    """Schemat argumentow ``read_note``."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ...,
        min_length=1,
        description=(
            "Sciezka relatywna do notatki .md w vaulcie, np. 'modules/Auth.md'. "
            "Akceptuje tylko faktyczne sciezki — stem (np. 'Auth') nie przejdzie; "
            "do fuzzy lookupu uzyj find_related."
        ),
    )
    sections: list[str] | None = Field(
        default=None,
        description=(
            "Opcjonalna lista tytulow sekcji do zwrocenia (bez '#'). Gdy podane — "
            "body jest filtrowane tylko do tych sekcji. Brakujace sekcje zgloszone "
            "w 'missing_sections'. Domyslnie None = pelne body."
        ),
    )


class ReadNoteTool(Tool):
    """Zwraca tresc notatki (frontmatter + body + in/out-linki), opcjonalnie tylko wybrane sekcje."""

    name = "read_note"
    description = (
        "Czyta pojedyncza notatke .md z vaulta. Zwraca {frontmatter, body, "
        "wikilinks_out, wikilinks_in, title}. Argument 'sections' pozwala pobrac "
        "tylko wybrane sekcje (po tytule) zamiast calego body — oszczedza tokeny "
        "na duzych hubach. Uwzglednia pending writes z tej sesji (widzisz "
        "efektywny stan, nie tylko to co na dysku)."
    )

    def input_schema(self) -> dict[str, Any]:
        return _ReadNoteArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _ReadNoteArgs.model_validate(args)
        except Exception as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        normalized = normalize_path_or_error(parsed.path)
        if isinstance(normalized, ToolResult):
            return normalized

        if not path_exists_effectively(ctx, normalized):
            return ToolResult(
                ok=False,
                error=(
                    f"note not found: {normalized!r}. Uzyj list_notes albo find_related "
                    f"zeby znalezc istniejaca sciezke."
                ),
            )

        content = compute_effective_content(ctx, normalized)
        if content is None:
            return ToolResult(
                ok=False,
                error=f"Nie udalo sie odczytac biezacej tresci {normalized!r}.",
            )

        try:
            parts = parse_frontmatter(content)
        except MarkdownOpsError as exc:
            return ToolResult(
                ok=False,
                error=f"Frontmatter {normalized!r} ma zepsuty YAML: {exc}",
            )

        frontmatter = parts.data or {}
        body = parts.body

        missing_sections: list[str] = []
        if parsed.sections:
            body, missing_sections = _extract_sections(body, parsed.sections)

        knowledge = ctx.ensure_vault_knowledge()
        note = knowledge.get(normalized)

        if note is not None:
            wikilinks_out = list(note.wikilinks)
            title = note.title
        else:
            wikilinks_out = []
            title = None

        stem = normalized.rsplit("/", 1)[-1]
        if stem.endswith(".md"):
            stem = stem[:-3]
        wikilinks_in = knowledge.wikilinks_in(stem)

        structured: dict[str, Any] = {
            "path": normalized,
            "title": title,
            "frontmatter": _stringify_frontmatter(frontmatter),
            "body": body,
            "wikilinks_out": wikilinks_out,
            "wikilinks_in": wikilinks_in,
        }
        if parsed.sections is not None:
            structured["requested_sections"] = list(parsed.sections)
            structured["missing_sections"] = missing_sections

        ctx.record_action(
            tool=self.name,
            path=normalized,
            args={
                "sections": list(parsed.sections) if parsed.sections else None,
                "body_chars": len(body),
                "wikilinks_out": len(wikilinks_out),
                "wikilinks_in": len(wikilinks_in),
            },
            ok=True,
        )

        content_text = _render_model_text(structured)
        return ToolResult(ok=True, content=content_text, structured=structured)


def _extract_sections(body: str, wanted: list[str]) -> tuple[str, list[str]]:
    """Zwraca body ograniczone do wskazanych sekcji + liste brakujacych.

    Matching po tytule (trim, case-sensitive), niezalezny od poziomu (``##`` vs ``###``).
    Zachowuje oryginalne linie (heading + body sekcji az do nastepnego headingu
    rownego lub wyzszego poziomu).

    Brakujace sekcje trafiaja do ``missing`` — caller zglasza je modelowi,
    zeby mogl sie poprawic w kolejnej iteracji.
    """

    wanted_set = {s.strip() for s in wanted if s and s.strip()}
    if not wanted_set:
        return body, []

    lines, had_trailing = _split_lines_preserving(body)
    in_fence = _iter_code_fence_mask(lines)
    spans = _find_heading_spans(lines, in_fence)

    chosen_lines: list[int] = []
    found: set[str] = set()
    for span in spans:
        if span.title in wanted_set:
            found.add(span.title)
            for i in range(span.heading_line, span.body_end):
                chosen_lines.append(i)

    missing = sorted(s for s in wanted_set if s not in found)

    if not chosen_lines:
        return "", missing

    chosen_set = set(chosen_lines)
    output = [lines[i] for i in range(len(lines)) if i in chosen_set]
    return _join_lines(output, had_trailing), missing


def _stringify_frontmatter(data: dict[str, Any]) -> dict[str, Any]:
    """Konwertuje nie-JSON-owe wartosci (datetime, date) na stringi.

    ``VaultNote.frontmatter`` moze zawierac obiekty ``datetime``/``date``
    po parsowaniu przez ``VaultManager``. Do JSON-a trzeba je spłaszczyc —
    model zobaczy ISO stringi. Listy i dicty sa rekurencyjne (ale plytko —
    frontmatter nie ma zwykle gleboko zagniezdzonych struktur).
    """

    import datetime as _dt

    def _coerce(value: Any) -> Any:
        if isinstance(value, (_dt.datetime, _dt.date)):
            return value.isoformat()
        if isinstance(value, list):
            return [_coerce(v) for v in value]
        if isinstance(value, dict):
            return {str(k): _coerce(v) for k, v in value.items()}
        return value

    return {str(k): _coerce(v) for k, v in data.items()}


def _render_model_text(structured: dict[str, Any]) -> str:
    """Buduje plain-text odpowiedz dla modelu — markdown-friendly.

    Strukturyzacja lekka (nagowki +  codefence YAML + body) zamiast JSON-a —
    model lepiej rozumie naturalny format markdown niz dump JSON-a z
    escapowaniem.
    """

    import yaml as _yaml

    lines: list[str] = []
    lines.append(f"# read_note: {structured['path']}")
    if structured.get("title"):
        lines.append(f"_title: {structured['title']}_")
    lines.append("")

    frontmatter = structured.get("frontmatter") or {}
    if frontmatter:
        yaml_text = _yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).rstrip()
        lines.append("## frontmatter")
        lines.append("")
        lines.append("```yaml")
        lines.append(yaml_text)
        lines.append("```")
        lines.append("")

    body = structured.get("body") or ""
    lines.append("## body")
    lines.append("")
    if body:
        lines.append(body.rstrip())
    else:
        lines.append("_(puste)_")
    lines.append("")

    out_links = structured.get("wikilinks_out") or []
    in_links = structured.get("wikilinks_in") or []
    lines.append("## linki")
    lines.append(f"- wikilinks_out ({len(out_links)}): {', '.join(out_links) if out_links else '-'}")
    lines.append(f"- wikilinks_in ({len(in_links)}): {', '.join(in_links) if in_links else '-'}")

    missing = structured.get("missing_sections")
    if missing:
        lines.append("")
        lines.append(f"## missing_sections: {', '.join(missing)}")

    return "\n".join(lines)


__all__ = ["ReadNoteTool"]
