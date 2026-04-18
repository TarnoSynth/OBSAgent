"""``list_tags`` — mapa tagow vaulta z licznikami (Faza 5 refaktoru: nawigacja po metadanych).

**Semantyka:**

Zwraca liste unikalnych tagow z liczba notatek per tag, opcjonalnie
przefiltrowana po ``path_prefix`` / ``type``. Gdy ``include_top_paths=true``,
dla kazdego tagu zwracamy do 3 reprezentatywnych ``path`` — model czesto
dzieki temu moze pominac dodatkowy ``list_notes``.

**Dlaczego to jest tanie:**

Wszystko leci po ``VaultKnowledge.by_tag`` — inverted index zbudowany raz
na sesje. Brak re-parsowania plikow, brak I/O. Typowy wynik dla vaulta
500 notatek + 50 tagow = 1-2 kB JSON-a.

**Zastosowanie w prompt-ownym flow:**

Model widzi w prompcie tylko TOP-N tagow (``_MAX_TOP_TAGS``). Gdy potrzebuje
pelnej mapy (np. zeby sprawdzic czy tag 'legacy' istnieje, bo nie ma go w
top-15) — wola ``list_tags``. To pojedyncze wywolanie zamiast zgadywania i
dumpowania ``list_notes`` bez filtrow.

**Determinizm:**

Sortowanie malejaco po ``count``, remisy rozstrzygane alfabetycznie po
nazwie tagu. Daje stabilny prefiks dla prompt cachingu.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext

_DEFAULT_LIMIT = 30
_MAX_LIMIT = 200
_MAX_TOP_PATHS = 3


class _ListTagsArgs(BaseModel):
    """Schemat argumentow ``list_tags``."""

    model_config = ConfigDict(extra="forbid")

    path_prefix: str | None = Field(
        default=None,
        description=(
            "Opcjonalny prefix sciezki — ogranicza policzone tagi do notatek z "
            "danego folderu, np. 'modules/' albo 'adr'. Slash terminujacy opcjonalny."
        ),
    )
    type: str | None = Field(
        default=None,
        description=(
            "Opcjonalny filtr typu — liczy tagi tylko w notatkach o ``type=<value>`` "
            "(np. 'module', 'ADR'). Case-sensitive zgodnie z frontmatterem."
        ),
    )
    min_count: int = Field(
        default=1,
        ge=1,
        le=1000,
        description="Pomin tagi, ktore wystepuja w mniej niz N notatkach. Domyslnie 1 (wszystkie).",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=_MAX_LIMIT,
        description=(
            f"Gorny limit tagow w odpowiedzi (top-N po count). Domyslnie {_DEFAULT_LIMIT}, "
            f"maks {_MAX_LIMIT}. Zwracane sa zawsze najliczniejsze."
        ),
    )
    include_top_paths: bool = Field(
        default=True,
        description=(
            f"Gdy True — dla kazdego tagu dolacz do {_MAX_TOP_PATHS} reprezentatywnych "
            f"sciezek (alfabetycznie). Oszczedza dodatkowe wywolanie list_notes."
        ),
    )


class ListTagsTool(Tool):
    """Zwraca mape tagow z licznikami + opcjonalnie reprezentatywne notatki per tag."""

    name = "list_tags"
    description = (
        "Mapa tagow w vaulcie: {tag: count, top_paths}. Uzyj zanim zrobisz "
        "list_notes bez filtrow — zobaczysz caly landscape tagow i od razu "
        "zawezisz przez list_notes(tag=...). Obsluguje filtr path_prefix/type "
        "(np. tagi tylko w 'modules/'). Tanie — leci z indeksu w pamieci."
    )

    def input_schema(self) -> dict[str, Any]:
        return _ListTagsArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _ListTagsArgs.model_validate(args)
        except Exception as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        knowledge = ctx.ensure_vault_knowledge()
        limit = parsed.limit if parsed.limit is not None else _DEFAULT_LIMIT

        notes = knowledge.notes
        if parsed.path_prefix:
            prefix = parsed.path_prefix
            notes = [n for n in notes if n.path.startswith(prefix)]
        if parsed.type:
            type_filter = parsed.type.strip()
            notes = [n for n in notes if n.type == type_filter]

        counter: Counter[str] = Counter()
        paths_per_tag: dict[str, list[str]] = {}
        for note in notes:
            for tag in note.tags:
                counter[tag] += 1
                if parsed.include_top_paths:
                    paths_per_tag.setdefault(tag, []).append(note.path)

        filtered = [(tag, cnt) for tag, cnt in counter.items() if cnt >= parsed.min_count]
        filtered.sort(key=lambda pair: (-pair[1], pair[0]))
        total_unique = len(filtered)
        visible = filtered[:limit]

        items: list[dict[str, Any]] = []
        for tag, count in visible:
            entry: dict[str, Any] = {"tag": tag, "count": count}
            if parsed.include_top_paths:
                top_paths = sorted(paths_per_tag.get(tag, []))[:_MAX_TOP_PATHS]
                entry["top_paths"] = top_paths
            items.append(entry)

        truncated = total_unique > len(visible)

        ctx.record_action(
            tool=self.name,
            path=None,
            args={
                "path_prefix": parsed.path_prefix,
                "type": parsed.type,
                "min_count": parsed.min_count,
                "limit": limit,
                "include_top_paths": parsed.include_top_paths,
                "returned": len(items),
                "total_unique": total_unique,
            },
            ok=True,
        )

        scope_bits: list[str] = []
        if parsed.path_prefix:
            scope_bits.append(f"path_prefix='{parsed.path_prefix}'")
        if parsed.type:
            scope_bits.append(f"type='{parsed.type}'")
        scope_str = f" scope: {', '.join(scope_bits)}" if scope_bits else ""
        header = f"list_tags: {len(items)}/{total_unique} tagow{scope_str}"
        if truncated:
            header += " (truncated — podnies 'limit' lub zaweza filtrami)"
        body = json.dumps(items, ensure_ascii=False, indent=2)
        content = f"{header}\n{body}"

        return ToolResult(
            ok=True,
            content=content,
            structured={
                "total_unique": total_unique,
                "returned": len(items),
                "truncated": truncated,
                "items": items,
            },
        )


__all__ = ["ListTagsTool"]
