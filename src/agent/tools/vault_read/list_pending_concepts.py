"""``list_pending_concepts`` - orphan wikilinki + indeks rejestrowanych placeholderow.

**Semantyka (Faza 6):**

Zwraca ujednolicona liste "niespelnionych" pojec w vaulcie — unia dwoch zrodel:

1. **Auto-detekcja:** orphan wikilinki z ``VaultKnowledge.orphan_wikilinks()``
   — ``[[X]]`` wzmiankowane w cialach notatek, ale bez odpowiadajacego pliku.
2. **Swiadoma rejestracja:** tabela z ``_Pending_Concepts.md`` — wpisy
   dopisane przez ``register_pending_concept`` (Faza 6). Tutaj user/agent
   oznacza "wiem ze tego jeszcze nie ma, ale chce miec slad".

**Merge:**

Dla kazdego pojecia dajemy jeden wpis z polami:

- ``target`` -- nazwa (stem) pojecia,
- ``mentioned_in`` -- lista sciezek zrodlowych (unia auto-detekcji + indeksu),
- ``mentioned_count`` -- ``len(mentioned_in)``,
- ``registered`` -- ``True`` gdy obecny w ``_Pending_Concepts.md``,
- ``resolved`` -- ``True`` gdy ``target`` MA juz plik w vaulcie, a mimo to
  wciaz jest w tabeli placeholderow (sygnal dla reconciliacji: user/agent
  moze usunac wiersz recznie albo agent zostawia hint w summary).
- ``hint`` -- opcjonalny hint z tabeli (``None`` gdy brak albo auto-only).

**Typowe scenariusze uzycia przez model:**

1. **Przed utworzeniem notatki:** sprawdz, czy pojecie jest znanym
   placeholderem ("o, ``[[Qdrant]]`` jest w 3 modulach jako orphan —
   commit wlasnie dodaje Qdrant, stworze ``create_technology``").
2. **Przed dopisaniem orphan wikilinku:** zarejestruj go swiadomie
   przez ``register_pending_concept`` zamiast zostawiac niemy orphan.
3. **Reconciliation:** po utworzeniu ``Qdrant.md`` ta funkcja oznaczy
   wpis jako ``resolved=True`` — agent moze zaproponowac usuniecie wiersza
   albo pominac, user sam sprzata.

**Pusty wynik:**

Vault bez orphan wikilinkow i z pusta tabela placeholderow zwraca ``[]``.
Vault z resolved-only placeholderami (wpisy w tabeli, ale wszystkie
maja juz notatki) zwraca wpisy z ``resolved=True`` — sygnal, ze user
moze recznie wyczyscic indeks.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext
from src.agent.tools.vault_write._markdown_ops import (
    _iter_code_fence_mask,
    _parse_pipe_row,
    _split_lines_preserving,
    _TABLE_SEPARATOR_RE,
    find_heading_span,
)
from src.agent.tools.vault_write.register_pending_concept import (
    PENDING_CONCEPTS_PATH,
    PENDING_CONCEPTS_SECTION,
)


class _ListPendingConceptsArgs(BaseModel):
    """``list_pending_concepts`` nie przyjmuje zadnych argumentow.

    Pusty model (z ``extra='forbid'``) sygnalizuje modelowi i providerowi,
    ze tool nie potrzebuje inputu — wywolanie ``list_pending_concepts({})``
    jest OK i preferowane.
    """

    model_config = ConfigDict(extra="forbid")


def _unescape_cell(text: str) -> str:
    return text.replace(r"\|", "|").strip()


def _read_registered_entries(ctx: ToolExecutionContext) -> dict[str, dict[str, Any]]:
    """Parsuje tabele z ``_Pending_Concepts.md`` do mapy ``{name: {sources, hint}}``.

    Nie rzuca — gdy plik nie istnieje albo tabela jest uszkodzona, zwracamy
    ``{}``. ``list_pending_concepts`` jest read-only, wiec bledy parse'a
    uznajemy za brak rejestracji, a nie domain error do modelu.

    Czytamy przez ``VaultManager.read_text`` (nie przez pending writes), bo
    w Fazie 6 sesje mozna wolac narzedzie zarowno po ``register_pending_concept``
    (widzimy pending update), jak i na czyscu. W praktyce uzywamy
    ``compute_effective_content``, zeby model po rejestracji zobaczyl
    pending wpis — inaczej mogloby sie zdawac, ze duplikuje.
    """

    from src.agent.tools.vault_write._granular import compute_effective_content

    content = compute_effective_content(ctx, PENDING_CONCEPTS_PATH)
    if not content:
        return {}

    span = find_heading_span(content, PENDING_CONCEPTS_SECTION)
    if span is None:
        return {}

    lines, _ = _split_lines_preserving(content)
    in_fence = _iter_code_fence_mask(lines)

    sep_idx: int | None = None
    i = span.body_start
    while i < span.body_end - 1:
        if in_fence[i]:
            i += 1
            continue
        if _parse_pipe_row(lines[i]) is not None and i + 1 < span.body_end:
            if _TABLE_SEPARATOR_RE.match(lines[i + 1]):
                sep_idx = i + 1
                break
        i += 1

    if sep_idx is None:
        return {}

    entries: dict[str, dict[str, Any]] = {}
    for j in range(sep_idx + 1, span.body_end):
        if in_fence[j]:
            break
        cells = _parse_pipe_row(lines[j])
        if cells is None or not cells:
            break
        name = _unescape_cell(cells[0])
        if not name:
            continue
        sources_raw = _unescape_cell(cells[1]) if len(cells) > 1 else ""
        sources = [s.strip() for s in sources_raw.split(",") if s.strip()]
        hint = _unescape_cell(cells[2]) if len(cells) > 2 else ""
        entries[name] = {
            "sources": sources,
            "hint": hint or None,
        }
    return entries


class ListPendingConceptsTool(Tool):
    """Lista pending conceptow: unia auto-orphan wikilinkow + wpisow z ``_Pending_Concepts.md``."""

    name = "list_pending_concepts"
    description = (
        "Zwraca unia dwoch zrodel placeholderow: (a) orphan wikilinkow auto-wykrytych "
        "z vaulta (`[[X]]` wzmiankowanych bez pliku), (b) swiadomych rejestracji z "
        "`_Pending_Concepts.md` dopisanych przez `register_pending_concept`. "
        "Per wpis: target, mentioned_in[], mentioned_count, registered, resolved, hint. "
        "Uzywaj, zeby wykryc luki w grafie wiedzy zanim utworzysz nowa notatke lub "
        "dopiszesz orphan link."
    )

    def input_schema(self) -> dict[str, Any]:
        return _ListPendingConceptsArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            _ListPendingConceptsArgs.model_validate(args)
        except Exception as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        knowledge = ctx.ensure_vault_knowledge()
        auto_mapping = knowledge.orphan_wikilinks()
        registered = _read_registered_entries(ctx)

        existing_stems: set[str] = set(knowledge.by_stem.keys())

        merged: dict[str, dict[str, Any]] = {}

        for target, sources in auto_mapping.items():
            merged[target] = {
                "target": target,
                "mentioned_in": sorted(set(sources)),
                "registered": False,
                "resolved": False,
                "hint": None,
            }

        for name, entry in registered.items():
            bucket = merged.get(name)
            registered_sources = entry["sources"]
            if bucket is None:
                bucket = {
                    "target": name,
                    "mentioned_in": [],
                    "registered": True,
                    "resolved": name in existing_stems,
                    "hint": entry["hint"],
                }
                merged[name] = bucket
            else:
                bucket["registered"] = True
                bucket["resolved"] = name in existing_stems
                if entry["hint"] and not bucket["hint"]:
                    bucket["hint"] = entry["hint"]

            combined = list(bucket["mentioned_in"])
            for src in registered_sources:
                if src not in combined:
                    combined.append(src)
            bucket["mentioned_in"] = sorted(combined)

        items: list[dict[str, Any]] = []
        for key in sorted(merged.keys()):
            bucket = merged[key]
            bucket["mentioned_count"] = len(bucket["mentioned_in"])
            items.append(bucket)

        ctx.record_action(
            tool=self.name,
            path=None,
            args={
                "returned": len(items),
                "auto_orphans": len(auto_mapping),
                "registered_in_index": len(registered),
                "resolved_in_index": sum(1 for it in items if it["resolved"]),
            },
            ok=True,
        )

        if not items:
            return ToolResult(
                ok=True,
                content="list_pending_concepts: 0 orphan wikilinkow i 0 zarejestrowanych placeholderow (graf spojny).",
                structured={"returned": 0, "items": []},
            )

        header = (
            f"list_pending_concepts: {len(items)} wpis(y) — "
            f"auto={len(auto_mapping)}, registered={len(registered)}, "
            f"resolved_in_index={sum(1 for it in items if it['resolved'])}"
        )
        body = json.dumps(items, ensure_ascii=False, indent=2)
        return ToolResult(
            ok=True,
            content=f"{header}\n{body}",
            structured={"returned": len(items), "items": items},
        )


__all__ = ["ListPendingConceptsTool"]
