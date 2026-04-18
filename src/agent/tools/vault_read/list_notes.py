"""``list_notes`` — filtrowana lista notatek z vaulta (Faza 4 + Faza 5 refaktoru).

**Semantyka:**

Zwraca liste notatek pasujacych do filtrow ``type`` / tagi / ``parent`` /
``path_prefix`` (AND miedzy roznymi kategoriami filtrow). Bez filtrow zwraca
wszystkie notatki z limitem (domyslnie 50).

**Filtr tagow (Faza 5 — multi-tag):**

- ``tag``       — pojedynczy (backward-compat, rownowazny ``tags_any=[tag]``)
- ``tags_any``  — OR (notatka ma dowolny z listy)
- ``tags_all``  — AND (notatka ma wszystkie z listy)
- ``tags_none`` — NOT (notatka nie ma zadnego z listy)

Semantyka miedzy kategoriami: ``(tags_any) AND (tags_all) AND NOT(tags_none)``.

**Co zwraca na wpis notatki:**

Bazowo: ``{path, title, type, tags, parent}``. Z ``include_preview=true`` dodatkowo
``preview`` — pierwsze ~200 znakow body po frontmatterze (oczyszczone z leading
headingow). To eliminuje wiekszosc "rekonesansowych" wywolan ``read_note``.
Pelna tresc notatki (body + frontmatter + wikilinks_in/out) dalej przez
``read_note(path)``.

**Dlaczego limit domyslnie 50:**

Typowa sesja model LLM analizujacego pojedynczy commit potrzebuje przejrzec
10-30 notatek maks. 50 daje margines bez wypychania kontekstu z tokenami.
Model moze podniesc limit jawnie argumentem (max 500 — twardy cap), ale
w praktyce lepiej **zaweza filtry** niz prosi o wiecej wynikow.

**Determinizm:**

Wyniki posortowane alfabetycznie po ``path``. Prompt caching (Anthropic)
moze cachowac stabilne prefiksy konwersacji, wiec chcemy miec powtarzalny
output przy tych samych argumentach.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500
_DEFAULT_PREVIEW_CHARS = 200
_MAX_PREVIEW_CHARS = 800


def _normalize_tag(tag: str) -> str:
    """Normalizuje tag do postaci porownywalnej (lowercase, bez '#')."""

    return tag.lstrip("#").strip().lower()


class _ListNotesArgs(BaseModel):
    """Schemat argumentow ``list_notes``."""

    model_config = ConfigDict(extra="forbid")

    type: str | None = Field(
        default=None,
        description=(
            "Filtruj po typie notatki (``type`` we frontmatterze), np. 'module', "
            "'ADR', 'decision', 'hub', 'concept', 'technology', 'changelog', 'doc'. "
            "Wartosci case-sensitive zgodnie z frontmatterem notatek."
        ),
    )
    tag: str | None = Field(
        default=None,
        description=(
            "Pojedynczy tag (backward-compat). Akceptuje 'auth' lub '#auth'. "
            "Dla wielu tagow uzyj 'tags_any' / 'tags_all' / 'tags_none'."
        ),
    )
    tags_all: list[str] | None = Field(
        default=None,
        description=(
            "Notatka musi miec WSZYSTKIE wymienione tagi (AND). Przyklad: "
            "['auth', 'security'] -> tylko notatki taggowane oboma. Prefix '#' opcjonalny."
        ),
    )
    tags_any: list[str] | None = Field(
        default=None,
        description=(
            "Notatka musi miec DOWOLNY z wymienionych tagow (OR). Przyklad: "
            "['auth', 'oauth', 'jwt'] -> unia. Prefix '#' opcjonalny."
        ),
    )
    tags_none: list[str] | None = Field(
        default=None,
        description=(
            "Notatka NIE moze miec zadnego z wymienionych tagow (NOT). Typowy "
            "case: ['archived', 'deprecated'] -> pomin nieaktualne."
        ),
    )
    parent: str | None = Field(
        default=None,
        description=(
            "Filtruj po polu ``parent`` we frontmatterze. Akceptuje stem lub wikilink — "
            "'MOC__Core', '[[MOC__Core]]', 'MOC___Kompendium' dzialaja rownowaznie."
        ),
    )
    path_prefix: str | None = Field(
        default=None,
        description=(
            "Filtruj po prefixie sciezki relatywnej wzgledem vaulta, np. 'modules/' "
            "albo 'adr'. Slash terminujacy opcjonalny. Match case-sensitive."
        ),
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=_MAX_LIMIT,
        description=(
            f"Gorny limit wynikow. Domyslnie {_DEFAULT_LIMIT}, maks {_MAX_LIMIT}. "
            "Preferuj zawezanie filtrow zamiast zwiekszania limitu."
        ),
    )
    include_preview: bool = Field(
        default=False,
        description=(
            "Gdy True — do kazdej notatki dolacz 'preview' (pierwsze N znakow body, "
            "po frontmatterze, oczyszczone z headingow poczatkowych). Eliminuje "
            "wiekszosc rekonesansowych wywolan read_note. Default False."
        ),
    )
    preview_chars: int | None = Field(
        default=None,
        ge=40,
        le=_MAX_PREVIEW_CHARS,
        description=(
            f"Dlugosc 'preview' w znakach. Domyslnie {_DEFAULT_PREVIEW_CHARS}, "
            f"maks {_MAX_PREVIEW_CHARS}. Ignorowane gdy include_preview=False."
        ),
    )


class ListNotesTool(Tool):
    """Zwraca liste notatek z vaulta po filtrach (read-only, cachowane per sesja)."""

    name = "list_notes"
    description = (
        "Lista notatek w vaulcie po filtrach: type/parent/path_prefix + "
        "multi-tag (tags_any/tags_all/tags_none) lub pojedynczy 'tag'. "
        "Zwraca {path, title, type, tags, parent} per wpis; z include_preview=true "
        "dodatkowo pierwsze ~200 znakow body (oszczedza osobne read_note). "
        "Uzyj zanim zaproponujesz nowa notatke. Domyslny limit 50."
    )

    def input_schema(self) -> dict[str, Any]:
        return _ListNotesArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _ListNotesArgs.model_validate(args)
        except Exception as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        knowledge = ctx.ensure_vault_knowledge()
        limit = parsed.limit if parsed.limit is not None else _DEFAULT_LIMIT
        preview_chars = (
            parsed.preview_chars if parsed.preview_chars is not None else _DEFAULT_PREVIEW_CHARS
        )

        notes = list(knowledge.notes)

        if parsed.type:
            type_filter = parsed.type.strip()
            notes = [n for n in notes if n.type == type_filter]

        tags_any_set: set[str] = set()
        if parsed.tag:
            tag_norm = _normalize_tag(parsed.tag)
            if tag_norm:
                tags_any_set.add(tag_norm)
        if parsed.tags_any:
            tags_any_set |= {_normalize_tag(t) for t in parsed.tags_any if t.strip()}
        if tags_any_set:
            notes = [
                n for n in notes
                if any(_normalize_tag(t) in tags_any_set for t in n.tags)
            ]

        if parsed.tags_all:
            tags_all_set = {_normalize_tag(t) for t in parsed.tags_all if t.strip()}
            if tags_all_set:
                notes = [
                    n for n in notes
                    if tags_all_set.issubset({_normalize_tag(t) for t in n.tags})
                ]

        if parsed.tags_none:
            tags_none_set = {_normalize_tag(t) for t in parsed.tags_none if t.strip()}
            if tags_none_set:
                notes = [
                    n for n in notes
                    if not any(_normalize_tag(t) in tags_none_set for t in n.tags)
                ]

        if parsed.parent:
            parent_key = knowledge._normalize_ref(parsed.parent)
            if parent_key:
                notes = [n for n in notes if (n.parent or "") == parent_key]

        if parsed.path_prefix:
            prefix = parsed.path_prefix
            notes = [n for n in notes if n.path.startswith(prefix)]

        notes.sort(key=lambda n: n.path)
        total = len(notes)
        notes = notes[:limit]

        items: list[dict[str, Any]] = []
        for n in notes:
            entry: dict[str, Any] = {
                "path": n.path,
                "title": n.title,
                "type": n.type,
                "tags": list(n.tags),
                "parent": n.parent,
            }
            if parsed.include_preview:
                entry["preview"] = _build_preview(n.content, preview_chars)
            items.append(entry)

        truncated = total > limit

        ctx.record_action(
            tool=self.name,
            path=None,
            args={
                "type": parsed.type,
                "tag": parsed.tag,
                "tags_all": list(parsed.tags_all) if parsed.tags_all else None,
                "tags_any": list(parsed.tags_any) if parsed.tags_any else None,
                "tags_none": list(parsed.tags_none) if parsed.tags_none else None,
                "parent": parsed.parent,
                "path_prefix": parsed.path_prefix,
                "limit": limit,
                "include_preview": parsed.include_preview,
                "preview_chars": preview_chars if parsed.include_preview else None,
                "returned": len(items),
                "total_before_limit": total,
            },
            ok=True,
        )

        header = f"list_notes: {len(items)}/{total} notatek"
        if truncated:
            header += f" (truncated — uzyj filtrow lub wyzszego limit={_MAX_LIMIT})"
        body = json.dumps(items, ensure_ascii=False, indent=2)
        content = f"{header}\n{body}"

        return ToolResult(
            ok=True,
            content=content,
            structured={
                "total": total,
                "returned": len(items),
                "truncated": truncated,
                "items": items,
            },
        )


def _build_preview(body: str, max_chars: int) -> str:
    """Zwraca czysty, krotki preview body notatki.

    Intent: model ma zobaczyc "o czym jest ta notatka" bez osobnego
    ``read_note``. Dlatego wycinamy:

    - leading whitespace
    - pierwsze headingi na gorze (``# Title``, ``## Section``) — zwykle
      duplikuja tytul/sekcje; nie niosa tresci
    - nadmiarowe puste linie (>2 pod rzad -> 1)

    Jesli po czyszczeniu tresc jest dluzsza niz ``max_chars`` — tnie z
    tyłu i dodaje ellipsis. ``body`` bez frontmattera (w ``VaultNote.content``
    frontmatter juz odciety).
    """

    if not body:
        return ""

    lines = body.split("\n")

    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    while idx < len(lines) and lines[idx].lstrip().startswith("#"):
        idx += 1
        while idx < len(lines) and not lines[idx].strip():
            idx += 1

    rest = "\n".join(lines[idx:]).strip()
    if not rest:
        rest = body.strip()

    collapsed: list[str] = []
    blank_streak = 0
    for ln in rest.split("\n"):
        if ln.strip() == "":
            blank_streak += 1
            if blank_streak <= 1:
                collapsed.append("")
        else:
            blank_streak = 0
            collapsed.append(ln)
    rest = "\n".join(collapsed).strip()

    if len(rest) <= max_chars:
        return rest
    return rest[: max_chars - 1].rstrip() + "…"


__all__ = ["ListNotesTool"]
