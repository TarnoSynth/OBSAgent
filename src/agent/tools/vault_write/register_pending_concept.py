"""``register_pending_concept`` - rejestracja orphan wikilinku jako placeholdera (Faza 6).

**Cel Fazy 6:**

Orphan wikilinki (`[[X]]` wzmiankowane, ale bez pliku) przestaja byc niemym
bledem. Staja sie **pierwszoklasowymi placeholderami** — agent je rejestruje
w dedykowanej notatce-indeksie ``_Pending_Concepts.md``, a user/agent ma
zywa liste "rzeczy do wypelnienia" w vaulcie.

**Semantyka:**

Tool dopisuje wiersz do tabeli w ``_Pending_Concepts.md``:

- ``Nazwa``          -- pojecie (stem, bez ``[[ ]]``, bez aliasu ``|``, bez ``#``).
- ``Wzmiankowane w`` -- lista sciezek notatek, ktore wzmiankuja ``[[Nazwa]]``.
                        Wartosci oddzielone przecinkiem. Idempotentnie dokladane.
- ``Hint``           -- krotka notka od modelu "skad to sie wzielo" (opcjonalna).

**Idempotencja:**

- Drugi call z tym samym ``name`` + ``mentioned_in`` = no-op (nic nie dopisujemy).
- Drugi call z tym samym ``name`` + nowym ``mentioned_in`` = tylko rozszerzamy
  kolumne zrodel. ``hint`` z pierwszego calla pozostaje (nie nadpisujemy,
  zeby nie gubic pierwotnej intencji).
- ``name`` jest znormalizowany: ``"[[Mikroserwisy|ms]]"`` -> ``"Mikroserwisy"``.

**Auto-repair:**

Jesli plik ``_Pending_Concepts.md`` nie istnieje — tworzymy go z frontmatterem
``type: index`` + naglowkiem ``# Pending Concepts`` + sekcja ``## Placeholdery``
z pusta tabela. Jesli plik istnieje, ale brak sekcji / tabeli — dopisujemy je
(preserwujemy cala reszte).

**Wykluczenie z auto-MOC:**

``_Pending_Concepts.md`` ma ``type: index`` i jest explicite wykluczony z
``moc_planner.plan_post_action_updates`` (patrz ``PENDING_CONCEPTS_PATH``
w ``src.agent.tools.vault_write.register_pending_concept``) — agent nie
probuje dopisac go do zadnego MOC. To notatka-sluga, nie wezel merytoryczny.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext
from src.agent.tools.vault_write._common import normalize_path_or_error
from src.agent.tools.vault_write._granular import (
    compute_effective_content,
    register_granular_update,
)
from src.agent.tools.vault_write._markdown_ops import (
    MarkdownOpsError,
    _iter_code_fence_mask,
    _join_lines,
    _parse_pipe_row,
    _split_lines_preserving,
    _TABLE_SEPARATOR_RE,
    append_section,
    find_heading_span,
)


PENDING_CONCEPTS_PATH = "_Pending_Concepts.md"
"""Kanoniczna sciezka notatki-indeksu w vaulcie (relatywna do rootu vaulta).

Stala eksportowana, bo ``moc_planner`` i ``list_pending_concepts`` tez jej
potrzebuja — jedno zrodlo prawdy, zeby literowka nie rozsypala featury.
"""

PENDING_CONCEPTS_SECTION = "Placeholdery"
"""Tytul sekcji ``##`` w ``_Pending_Concepts.md`` pod ktora zyje tabela."""

_TABLE_HEADERS: tuple[str, str, str] = ("Nazwa", "Wzmiankowane w", "Hint")

_WIKILINK_RE = re.compile(r"^\[\[([^\]]+)\]\]$")

_INITIAL_CONTENT = (
    "---\n"
    "type: index\n"
    "tags: [index, pending-concepts]\n"
    "status: active\n"
    "---\n"
    "\n"
    "# Pending Concepts\n"
    "\n"
    "> Automatyczny indeks orphan wikilinkow — pojec wzmiankowanych w vaulcie,\n"
    "> ktore nie maja jeszcze wlasnej notatki. Agent dopisuje tu wpisy przez\n"
    "> `register_pending_concept`. Usun wiersz, gdy utworzysz docelowa notatke.\n"
    "\n"
    f"## {PENDING_CONCEPTS_SECTION}\n"
    "\n"
    f"| {' | '.join(_TABLE_HEADERS)} |\n"
    "| --- | --- | --- |\n"
)


def _ensure_section_and_table(content: str) -> str:
    """Gwarantuje, ze ``content`` ma sekcje ``## Placeholdery`` z pusta tabela.

    Trzy przypadki:

    1. Plik nie istnieje -> zwracamy ``_INITIAL_CONTENT`` (wolajacy uzywa
       tego jako bazy).
    2. Plik istnieje i ma sekcje + tabele -> zwracamy niezmieniony.
    3. Plik istnieje, ale brak sekcji / brak tabeli w sekcji -> dopisujemy
       sekcje z pusta tabela na koncu pliku (``append_section``).
    """

    if not content.strip():
        return _INITIAL_CONTENT

    span = find_heading_span(content, PENDING_CONCEPTS_SECTION)
    if span is None:
        table_body = (
            f"| {' | '.join(_TABLE_HEADERS)} |\n"
            "| --- | --- | --- |"
        )
        return append_section(content, PENDING_CONCEPTS_SECTION, table_body, level=2)

    lines, _ = _split_lines_preserving(content)
    in_fence = _iter_code_fence_mask(lines)
    has_table = False
    i = span.body_start
    while i < span.body_end - 1:
        if not in_fence[i] and _parse_pipe_row(lines[i]) is not None:
            if i + 1 < span.body_end and _TABLE_SEPARATOR_RE.match(lines[i + 1]):
                has_table = True
                break
        i += 1

    if has_table:
        return content

    new_lines = (
        lines[: span.body_end]
        + [""]
        + [f"| {' | '.join(_TABLE_HEADERS)} |", "| --- | --- | --- |"]
        + lines[span.body_end:]
    )
    had_trailing = content.endswith("\n")
    return _join_lines(new_lines, had_trailing)


def _clean_concept_name(raw: str) -> str:
    """Normalizuje ``raw`` do samego stem-a: bez ``[[ ]]``, bez ``|alias``, bez ``#anchor``."""

    text = raw.strip()
    match = _WIKILINK_RE.match(text)
    if match:
        text = match.group(1)
    text = text.split("|", 1)[0].split("#", 1)[0].strip()
    return text


def _escape_cell(text: str) -> str:
    """Escape pipe w komorce GFM (identyczny kontrakt jak ``add_table_row``)."""

    return text.replace("|", r"\|").strip()


def _unescape_cell(text: str) -> str:
    return text.replace(r"\|", "|").strip()


def _split_mentioned_cell(cell: str) -> list[str]:
    """Parsuje ``"a.md, b.md"`` -> ``["a.md", "b.md"]`` (dedup, bez pustych)."""

    raw = _unescape_cell(cell)
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",")]
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            result.append(part)
    return result


def _join_mentioned_cell(sources: list[str]) -> str:
    return ", ".join(sources)


def _upsert_row(
    content: str,
    name: str,
    mentioned_in: str,
    hint: str | None,
) -> tuple[str, str]:
    """Dodaje lub rozszerza wiersz w tabeli. Zwraca ``(new_content, outcome)``.

    Gwarantowana ivariant wejsciowy: ``content`` ma juz sekcje + tabele (po
    ``_ensure_section_and_table``). W funkcji szukamy tabeli, lokalizujemy
    wiersz po kolumnie ``Nazwa`` (case-insensitive, po strip), i:

    - brak wiersza -> dopisujemy nowy po ostatnim wierszu danych,
      outcome = ``"added"``.
    - wiersz jest, ``mentioned_in`` juz w kolumnie zrodel -> no-op,
      outcome = ``"noop_already_present"``. Content zwracany niezmieniony.
    - wiersz jest, ``mentioned_in`` nowy -> podmieniamy wiersz in-place z
      rozszerzona lista zrodel, outcome = ``"source_added"``. ``hint``
      pozostaje z oryginalnego wpisu (nie nadpisujemy).
    """

    lines, had_trailing = _split_lines_preserving(content)
    in_fence = _iter_code_fence_mask(lines)

    span = find_heading_span(content, PENDING_CONCEPTS_SECTION)
    if span is None:
        raise MarkdownOpsError(
            f"Nie znaleziono sekcji '{PENDING_CONCEPTS_SECTION}' po ensure — "
            f"niespojnosc wewnetrzna."
        )

    header_idx: int | None = None
    sep_idx: int | None = None
    i = span.body_start
    while i < span.body_end - 1:
        if in_fence[i]:
            i += 1
            continue
        header_cells = _parse_pipe_row(lines[i])
        if header_cells is None:
            i += 1
            continue
        if i + 1 < span.body_end and _TABLE_SEPARATOR_RE.match(lines[i + 1]):
            header_idx = i
            sep_idx = i + 1
            break
        i += 1

    if header_idx is None or sep_idx is None:
        raise MarkdownOpsError(
            "Tabela placeholderow znikla po ensure — niespojnosc wewnetrzna."
        )

    last_data_idx = sep_idx
    j = sep_idx + 1
    while j < span.body_end:
        if in_fence[j]:
            break
        row_cells = _parse_pipe_row(lines[j])
        if row_cells is None:
            break
        last_data_idx = j
        j += 1

    clean_name_cmp = name.strip().lower()
    matched_idx: int | None = None
    matched_cells: list[str] | None = None
    for k in range(sep_idx + 1, last_data_idx + 1):
        row_cells = _parse_pipe_row(lines[k])
        if row_cells is None:
            continue
        if not row_cells:
            continue
        first = _unescape_cell(row_cells[0]).strip().lower()
        if first == clean_name_cmp:
            matched_idx = k
            matched_cells = row_cells
            break

    if matched_idx is not None and matched_cells is not None:
        existing_sources = _split_mentioned_cell(matched_cells[1] if len(matched_cells) > 1 else "")
        if mentioned_in in existing_sources:
            return content, "noop_already_present"
        existing_sources.append(mentioned_in)
        existing_hint = _unescape_cell(matched_cells[2]) if len(matched_cells) > 2 else ""
        new_cells = [
            _escape_cell(name),
            _escape_cell(_join_mentioned_cell(existing_sources)),
            _escape_cell(existing_hint),
        ]
        lines[matched_idx] = "| " + " | ".join(new_cells) + " |"
        return _join_lines(lines, had_trailing), "source_added"

    new_cells = [
        _escape_cell(name),
        _escape_cell(mentioned_in),
        _escape_cell(hint or ""),
    ]
    new_row = "| " + " | ".join(new_cells) + " |"
    insert_at = last_data_idx + 1
    new_lines = lines[:insert_at] + [new_row] + lines[insert_at:]
    return _join_lines(new_lines, had_trailing), "added"


class _RegisterPendingConceptArgs(BaseModel):
    """Schemat argumentow ``register_pending_concept``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        min_length=1,
        description=(
            "Nazwa pojecia (stem notatki-docelowej). Akceptujemy '[[X]]' i 'X|alias' — "
            "zostaja znormalizowane do samego stem-a. Przyklad: 'Mikroserwisy'."
        ),
    )
    mentioned_in: str = Field(
        ...,
        min_length=1,
        description=(
            "Sciezka notatki wzmiankujacej, wzgledem rootu vaulta. Np. "
            "'hubs/Architektura_systemu.md'. Dopisywana do kolumny 'Wzmiankowane w'."
        ),
    )
    hint: str | None = Field(
        default=None,
        description=(
            "Opcjonalny krotki opis 'skad to sie wzielo' (1 zdanie). Np. "
            "'Pojecie wzmiankowane w kontekscie wyboru architektury.' "
            "Zachowywany tylko z pierwszego calla — kolejne nie nadpisuja."
        ),
    )


class RegisterPendingConceptTool(Tool):
    """Rejestruje orphan wikilink jako znany placeholder w ``_Pending_Concepts.md``."""

    name = "register_pending_concept"
    description = (
        "Rejestruje pojecie (orphan wikilink) jako znany placeholder w notatce "
        "indeksie '_Pending_Concepts.md'. Uzyj, gdy w swojej notatce chcesz "
        "wzmiankowac '[[X]]', ale notatka X jeszcze nie istnieje i nie masz "
        "czasu/kontekstu na jej pelne napisanie. Idempotentne — drugi call z tym "
        "samym name dopisuje tylko nowe zrodlo do kolumny 'Wzmiankowane w', "
        "nie duplikuje wierszy. Nic nie zapisuje natychmiast - finalizacja przez "
        "submit_plan."
    )

    def input_schema(self) -> dict[str, Any]:
        return _RegisterPendingConceptArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _RegisterPendingConceptArgs.model_validate(args)
        except Exception as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        name = _clean_concept_name(parsed.name)
        if not name:
            return ToolResult(
                ok=False,
                error="argument 'name' jest pusty po normalizacji (usunieciu [[ ]] / aliasow)",
            )

        mentioned_in = parsed.mentioned_in.strip()
        if not mentioned_in:
            return ToolResult(
                ok=False,
                error="argument 'mentioned_in' nie moze byc pusty",
            )

        hint = (parsed.hint or "").strip() or None

        normalized = normalize_path_or_error(PENDING_CONCEPTS_PATH)
        if isinstance(normalized, ToolResult):
            return normalized

        current = compute_effective_content(ctx, normalized)
        base = current if current is not None else ""

        try:
            prepared = _ensure_section_and_table(base)
            new_content, outcome = _upsert_row(prepared, name, mentioned_in, hint)
        except MarkdownOpsError as exc:
            ctx.record_action(
                tool=self.name,
                path=normalized,
                args={"name": name, "mentioned_in": mentioned_in},
                ok=False,
                error=str(exc),
            )
            return ToolResult(ok=False, error=str(exc))

        if outcome == "noop_already_present" and new_content == base:
            ctx.record_action(
                tool=self.name,
                path=normalized,
                args={
                    "name": name,
                    "mentioned_in": mentioned_in,
                    "result": "noop_already_present",
                },
                ok=True,
            )
            return ToolResult(
                ok=True,
                content=(
                    f"Pending concept '{name}' juz zarejestrowany z zrodlem "
                    f"{mentioned_in!r} - no-op (idempotencja)."
                ),
            )

        return register_granular_update(
            ctx=ctx,
            tool_name=self.name,
            normalized_path=normalized,
            new_content=new_content,
            op_summary=f"REGISTER_PENDING_CONCEPT '{name}' ({outcome})",
            extra_log_args={
                "name": name,
                "mentioned_in": mentioned_in,
                "hint_present": hint is not None,
                "outcome": outcome,
            },
        )


__all__ = [
    "PENDING_CONCEPTS_PATH",
    "PENDING_CONCEPTS_SECTION",
    "RegisterPendingConceptTool",
]
