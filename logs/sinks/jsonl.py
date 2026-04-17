"""JSONL sink — jeden plik per bieg, append-only.

Format: jeden event na linie, ``json.dumps(event.to_dict(), ensure_ascii=False)``.
Trivially grepowalny: ``rg '"phase":"FINALIZE"' logs/runs/*.jsonl``.

Bezpieczenstwo:

- Kazdy write jest ``flush()``owany, zeby Ctrl+C nie tracil konca pliku.
- Lock (``threading.Lock``) wokol pisania, bo eventy moga leciec z
  wielu tasków asyncio (jeden event loop = jeden watek, ale ``logging``
  z worker thread tez to wola — lepiej zabezpieczyc od razu).
- Wyjatki zamrozone w ``emit`` — tylko logujemy na stderr.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from logs.events import Event

_logger = logging.getLogger("obsagent.logs.jsonl")


def _default(o: Any) -> Any:
    """Fallback dla obiektow ktore nie sa JSON-able (nie powinny tu trafiac)."""
    try:
        return str(o)
    except Exception:
        return "<unserializable>"


class JsonlSink:
    """Zapisuje eventy jako JSONL do ``path`` (append). Tworzy parent dirs."""

    name = "jsonl"

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = self._path.open("a", encoding="utf-8", buffering=1)

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, event: Event) -> None:
        try:
            line = json.dumps(
                event.to_dict(),
                ensure_ascii=False,
                default=_default,
                separators=(",", ":"),
            )
        except Exception as exc:
            _logger.error("Nie udalo sie zserializowac eventu typu %r: %r", event.type, exc)
            return

        try:
            with self._lock:
                self._fh.write(line + "\n")
                self._fh.flush()
        except Exception as exc:
            _logger.error("Nie udalo sie zapisac eventu do %s: %r", self._path, exc)

    def close(self) -> None:
        try:
            with self._lock:
                if not self._fh.closed:
                    self._fh.flush()
                    self._fh.close()
        except Exception as exc:
            _logger.error("Blad przy zamykaniu %s: %r", self._path, exc)
