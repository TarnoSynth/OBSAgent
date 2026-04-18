"""``find_related`` — fuzzy search po vaulcie po temacie (Faza 4 refaktoru).

**Semantyka:**

Dla tekstu ``topic`` zwraca top-N notatek pasujacych po kilku sygnalach:

- **stem pliku** (np. ``Qdrant``)                         — waga 4.0
- **title notatki**                                        — waga 2.0
- **tagi**                                                 — waga 1.5
- **headingi w body** (``## Qdrant``, ``### Qdrant`` itp.) — waga 1.2
- **wikilinki wychodzace** (jesli inna notatka linkuje do tematu) — waga 0.8

**Zastosowanie (prompt):**

Gdy model widzi w commicie np. "wybralismy Qdrant", woła
``find_related(topic="Qdrant")`` zanim utworzy decision/technology —
zeby zobaczyc czy juz nie istnieje notatka (deduplikacja) i
jakie powiazane notatki moga stanowic ``related`` / ``parent``.

**Dlaczego ranker wagowy a nie full-text:**

Vault ma rzedy setek notatek — naiwny full-text (re.search po body)
zwalnia i halasuje. Ranker po strukturowanych polach (stem, title, tagi,
headingi) daje deterministyczne top-N i jest liniowy po rozmiarze vaulta,
bez external deps (rapidfuzz, whoosh). Wagi dobrane empirycznie pod
styl AthleteStack (gesty graf wikilinkow).

**Normalizacja query:**

Case-insensitive, stripped. Single token albo wieloslowowe frazy —
dla frazy liczymy jeden match dla substringa i jeden per slowo
(score sumuje sie). Spacje w topic nie lacza slow — to OR po tokenach.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext
from src.vault.models import VaultKnowledge, VaultNote

_DEFAULT_LIMIT = 10
_MAX_LIMIT = 50
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)

_WEIGHTS = {
    "stem": 4.0,
    "title": 2.0,
    "tag": 1.5,
    "heading": 1.2,
    "wikilink": 0.8,
}


class _FindRelatedArgs(BaseModel):
    """Schemat argumentow ``find_related``."""

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(
        ...,
        min_length=1,
        description=(
            "Temat do wyszukania — pojedyncze slowo (np. 'Qdrant') lub fraza "
            "('vector database'). Porownanie case-insensitive, po stem/title/"
            "tagach/headingach/wikilinkach."
        ),
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=_MAX_LIMIT,
        description=f"Gorny limit wynikow. Domyslnie {_DEFAULT_LIMIT}, maks {_MAX_LIMIT}.",
    )


class FindRelatedTool(Tool):
    """Wyszukuje notatki pasujace do tematu (fuzzy ranker po stem/title/tagach/headingach)."""

    name = "find_related"
    description = (
        "Wyszukuje top-N notatek pasujacych do 'topic' — po stem pliku, title, "
        "tagach, headingach sekcji, wikilinkach. Zwraca liste {path, title, type, "
        "score, matched_fields}. Uzywaj zanim zaproponujesz nowa notatke, zeby "
        "wykryc duplikaty i znalezc powiazania do pola 'related'."
    )

    def input_schema(self) -> dict[str, Any]:
        return _FindRelatedArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _FindRelatedArgs.model_validate(args)
        except Exception as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        topic = parsed.topic.strip()
        if not topic:
            return ToolResult(ok=False, error="'topic' nie moze byc pusty.")
        limit = parsed.limit if parsed.limit is not None else _DEFAULT_LIMIT

        knowledge = ctx.ensure_vault_knowledge()

        scored = _rank_notes(knowledge, topic)
        scored = [item for item in scored if item["score"] > 0.0]
        scored = scored[:limit]

        items: list[dict[str, Any]] = [
            {
                "path": item["path"],
                "title": item["title"],
                "type": item["type"],
                "score": round(item["score"], 3),
                "matched_fields": item["matched_fields"],
            }
            for item in scored
        ]

        ctx.record_action(
            tool=self.name,
            path=None,
            args={"topic": topic, "limit": limit, "returned": len(items)},
            ok=True,
        )

        header = f"find_related('{topic}'): {len(items)} wynikow"
        body = json.dumps(items, ensure_ascii=False, indent=2)
        return ToolResult(
            ok=True,
            content=f"{header}\n{body}",
            structured={
                "topic": topic,
                "returned": len(items),
                "items": items,
            },
        )


def _rank_notes(knowledge: VaultKnowledge, topic: str) -> list[dict[str, Any]]:
    """Zwraca notatki z policzonymi scorami — posortowane malejaco.

    Funkcja pure (poza znormalizowanym topicem). Unit test pokryje tu
    ze dla vaulta ``{Qdrant, pgvector, Model_embeddingowy}`` i topicu
    ``"vector database"`` top 3 zawiera wszystkie trzy.
    """

    topic_norm = topic.lower().strip()
    tokens = [tok for tok in re.split(r"\s+", topic_norm) if tok]
    if not tokens:
        return []

    candidates = {topic_norm, *tokens}

    results: list[dict[str, Any]] = []
    for note in knowledge.notes:
        score = 0.0
        matched: list[str] = []

        stem = _path_stem(note.path).lower()
        if _any_match(stem, candidates):
            score += _WEIGHTS["stem"]
            matched.append("stem")

        title = (note.title or "").lower()
        if title and _any_match(title, candidates):
            score += _WEIGHTS["title"]
            matched.append("title")

        tags_norm = [t.lower() for t in note.tags]
        if any(_any_match(t, candidates) for t in tags_norm):
            score += _WEIGHTS["tag"]
            matched.append("tag")

        for heading in _extract_headings(note.content):
            if _any_match(heading.lower(), candidates):
                score += _WEIGHTS["heading"]
                matched.append("heading")
                break

        for link in note.wikilinks:
            if _any_match(link.lower(), candidates):
                score += _WEIGHTS["wikilink"]
                matched.append("wikilink")
                break

        if score > 0.0:
            results.append({
                "path": note.path,
                "title": note.title,
                "type": note.type,
                "score": score,
                "matched_fields": matched,
            })

    results.sort(key=lambda x: (-x["score"], x["path"]))
    return results


def _extract_headings(body: str) -> list[str]:
    """Wyciaga tytuly headingow z body markdowna (bez '#')."""

    return [m.group(1).strip() for m in _HEADING_RE.finditer(body)]


def _path_stem(path: str) -> str:
    """Zwraca nazwe pliku bez rozszerzenia (odwzorowanie ``Path(path).stem``)."""

    base = path.rsplit("/", 1)[-1]
    if base.endswith(".md"):
        base = base[:-3]
    return base


def _any_match(haystack: str, needles: set[str]) -> bool:
    """Zwraca True jesli ktorykolwiek z ``needles`` jest substringiem ``haystack``.

    Pusty haystack albo pusty needle → False. Case-insensitive oczekiwane
    przez callera (oba argumenty juz lower-case).
    """

    if not haystack:
        return False
    return any(n for n in needles if n and n in haystack)


__all__ = ["FindRelatedTool"]
