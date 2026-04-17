"""Zdarzenia strukturalne biegu agenta — wspolny slownik dla wszystkich sinkow.

Zdarzenia sa niezmienne (``frozen=True``), serializowalne do JSON-u,
bez pow bez zadnych obiektow Pythonowych w ``payload`` (tylko typy JSON-able:
str / int / float / bool / None / dict / list). Konwersja do JSON-u
robiona jest w ``sinks.jsonl`` — tu nie robimy tego, bo konsolowy sink
moze chciec surowy dict.

Kontrakt ``type``: ``<domain>.<what>``. Grepowalne przez ``rg`` po JSONL.
Lista aktualnie uzywanych typow jest w ``EVENT_TYPES`` (check na
poziomie testow / sanity).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

EventLevel = Literal["debug", "info", "warning", "error"]


EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run.started",
        "run.ended",
        "commit.started",
        "commit.processed",
        "commit.rejected",
        "chunk.processed",
        "llm.call.started",
        "llm.call.ok",
        "llm.call.failed",
        "action.applied",
        "action.failed",
        "pending.approved",
        "pending.rejected",
        "vault.committed",
        "log",
    }
)


@dataclass(frozen=True)
class Event:
    """Pojedyncze zdarzenie w zyciu agenta.

    ``ts`` — ISO8601 z tz (UTC). ``run_id`` — identyfikator biegu z ``RunLogger``.
    ``payload`` — plaska mapa JSON-able; konwencja: klucze w snake_case.
    """

    type: str
    ts: datetime
    run_id: str
    level: EventLevel = "info"
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "ts": self.ts.astimezone(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "level": self.level,
            **self.payload,
        }


def utcnow() -> datetime:
    """Jednolity ts dla wszystkich eventow — aware UTC."""
    return datetime.now(tz=timezone.utc)
