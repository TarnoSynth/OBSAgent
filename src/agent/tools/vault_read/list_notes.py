"""``list_notes`` — filtrowana lista notatek z vaulta (Faza 4 refaktoru).

**Semantyka:**

Zwraca liste notatek pasujacych do filtrow ``type`` / ``tag`` / ``parent`` /
``path_prefix`` (AND miedzy filtrami). Bez filtrow zwraca wszystkie notatki,
ale z limitem (domyslnie 50) — model dostaje rozmowy glownie do uzywania
tego jako "sprawdz co juz mamy w module X" albo "pokaz wszystkie ADR-y".

**Co zwraca na wpis notatki:**

``{path, title, type, tags, parent}`` — dokladnie tyle, zeby model mogl sie
zorientowac "ta juz istnieje, nie tworz duplikatu" albo "zlinkuj ``[[X]]``".
Pelna tresc trzeba wziac przez ``read_note(path)``.

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
            "Filtruj po tagu — akceptuje 'auth' lub '#auth' (prefix '#' zostanie zdjety). "
            "Tag musi byc obecny w liscie ``tags`` notatki (unia frontmattera i body)."
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


class ListNotesTool(Tool):
    """Zwraca liste notatek z vaulta po filtrach (read-only, cachowane per sesja)."""

    name = "list_notes"
    description = (
        "Lista notatek w vaulcie przefiltrowana po type/tag/parent/path_prefix. "
        "Zwraca {path, title, type, tags, parent} per wpis. Uzyj na poczatku sesji "
        "zeby sprawdzic, co juz istnieje, zanim zaproponujesz nowa notatke lub "
        "wikilink. Domyslny limit 50 — zaweza wyniki przez filtry."
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

        notes = list(knowledge.notes)

        if parsed.type:
            type_filter = parsed.type.strip()
            notes = [n for n in notes if n.type == type_filter]

        if parsed.tag:
            tag_filter = parsed.tag.lstrip("#").strip().lower()
            if tag_filter:
                notes = [n for n in notes if any(t.lower() == tag_filter for t in n.tags)]

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

        items: list[dict[str, Any]] = [
            {
                "path": n.path,
                "title": n.title,
                "type": n.type,
                "tags": list(n.tags),
                "parent": n.parent,
            }
            for n in notes
        ]

        truncated = total > limit

        ctx.record_action(
            tool=self.name,
            path=None,
            args={
                "type": parsed.type,
                "tag": parsed.tag,
                "parent": parsed.parent,
                "path_prefix": parsed.path_prefix,
                "limit": limit,
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


__all__ = ["ListNotesTool"]
