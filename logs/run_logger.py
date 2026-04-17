"""RunLogger — kontener logowania JEDNEGO biegu agenta.

Cykl zycia:

1. ``RunLogger.create(config)`` w ``main.py`` — otwiera plik JSONL.
2. ``log_run_started`` — banner do konsoli + event do wszystkich sinkow.
3. Dla kazdego commita: ``log_commit_started`` / ``log_commit_processed``.
4. Dla kazdego wywolania LLM: helpery ``llm_started``/``llm_ok``/``llm_failed``
   — wywolywane z ``LoggingProvider`` wrappera.
5. ``close()`` (preferowanie przez ``with RunLogger(...) as rl:``) —
   flush + zamkniecie pliku.

Agent/main.py nie wola bezposrednio sinkow — zawsze przez RunLogger.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any

from logs.events import Event, EventLevel, utcnow
from logs.sinks.base import Sink
from logs.sinks.console import ConsoleSink
from logs.sinks.jsonl import JsonlSink

_logger = logging.getLogger("obsagent.logs.run")


def _new_run_id() -> str:
    """Krotkie, sortowalne id: YYYYMMDD-HHMMSS-<hex4>."""
    now = datetime.now(tz=timezone.utc)
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


class RunLogger:
    """Fasada logowania biegu. Trzyma liste sinkow i metadane runa.

    Nie jest thread-safe w sensie wspolbieznego ``emit`` z wielu watkow
    naraz (kazdy sink ma wlasny lock), ale kolejnosc eventow w JSONL
    moze sie przeplesc — nie polegaj na scisly kolejnosci miedzy watkami.
    Dla pojedynczego event loopa (asyncio) eventy ida w kolejnosci.
    """

    def __init__(
        self,
        *,
        run_id: str,
        sinks: list[Sink],
        project_name: str | None = None,
        jsonl_path: Path | None = None,
    ) -> None:
        self.run_id = run_id
        self.project_name = project_name
        self.jsonl_path = jsonl_path
        self._sinks = sinks
        self._started_at = utcnow()
        self._closed = False
        self._processed_count = 0
        self._error_count = 0

    @classmethod
    def create(
        cls,
        *,
        log_dir: Path,
        project_name: str | None = None,
        console_verbose: bool = False,
        enable_console: bool = True,
    ) -> "RunLogger":
        """Buduje RunLogger z domyslnymi sinkami.

        JSONL zawsze ON. Console ON (pokazuje tylko bledy + banner).
        """
        run_id = _new_run_id()
        log_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = log_dir / f"{run_id}.jsonl"

        sinks: list[Sink] = [JsonlSink(jsonl_path)]
        if enable_console:
            sinks.append(ConsoleSink(verbose=console_verbose))

        return cls(
            run_id=run_id,
            sinks=sinks,
            project_name=project_name,
            jsonl_path=jsonl_path,
        )

    def _emit(
        self,
        event_type: str,
        *,
        level: EventLevel = "info",
        **payload: Any,
    ) -> None:
        if self._closed:
            return
        event = Event(
            type=event_type,
            ts=utcnow(),
            run_id=self.run_id,
            level=level,
            payload=payload,
        )
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception as exc:
                _logger.error("Sink %s padl przy emit(%s): %r", sink.name, event_type, exc)

    # ------------------------------------------------------------------
    # public API wolane z main.py / agent.py
    # ------------------------------------------------------------------

    def log_run_started(
        self,
        *,
        provider: str,
        model: str,
        effort: str | None = None,
        project_repo: str,
        vault: str,
    ) -> None:
        self._emit(
            "run.started",
            provider=provider,
            model=model,
            effort=effort,
            project_repo=project_repo,
            vault=vault,
            jsonl_path=str(self.jsonl_path) if self.jsonl_path else None,
            project_name=self.project_name,
        )

    def log_run_ended(self, *, exit_code: int) -> None:
        self._emit(
            "run.ended",
            level="warning" if exit_code != 0 else "info",
            exit_code=exit_code,
            processed_count=self._processed_count,
            error_count=self._error_count,
            duration_s=(utcnow() - self._started_at).total_seconds(),
        )

    def log_commit_started(self, *, sha: str, author: str, subject: str) -> None:
        self._emit(
            "commit.started",
            sha=sha,
            sha_short=sha[:7],
            author=author,
            subject=subject,
        )

    def log_commit_processed(self, *, sha: str, vault_sha: str | None) -> None:
        self._processed_count += 1
        self._emit(
            "commit.processed",
            sha=sha,
            sha_short=sha[:7],
            vault_sha=vault_sha,
            vault_sha_short=vault_sha[:7] if vault_sha else None,
        )

    def log_commit_rejected(self, *, sha: str, reason: str) -> None:
        self._emit(
            "commit.rejected",
            level="warning",
            sha=sha,
            sha_short=sha[:7],
            reason=reason,
        )

    def log_chunk(
        self,
        *,
        sha: str,
        chunk_id: str,
        chunk_idx: int,
        chunk_total: int,
        files: list[str],
        hunk_count: int,
        line_count: int,
        cache_hit: bool,
    ) -> None:
        self._emit(
            "chunk.processed",
            sha=sha,
            sha_short=sha[:7],
            chunk_id=chunk_id,
            chunk_idx=chunk_idx,
            chunk_total=chunk_total,
            files=files,
            hunk_count=hunk_count,
            line_count=line_count,
            cache_hit=cache_hit,
        )

    def log_llm_started(self, **payload: Any) -> None:
        self._emit("llm.call.started", **payload)

    def log_llm_ok(self, **payload: Any) -> None:
        self._emit("llm.call.ok", **payload)

    def log_llm_failed(self, **payload: Any) -> None:
        self._error_count += 1
        self._emit("llm.call.failed", level="error", **payload)

    def log_action_applied(self, *, action_type: str, path: str) -> None:
        self._emit("action.applied", action_type=action_type, path=path)

    def log_action_failed(self, *, action_type: str, path: str, error: str) -> None:
        self._error_count += 1
        self._emit(
            "action.failed",
            level="error",
            action_type=action_type,
            path=path,
            error=error,
        )

    def log_pending(self, *, approved: bool, files: int) -> None:
        self._emit(
            "pending.approved" if approved else "pending.rejected",
            files=files,
        )

    def log_vault_commit(self, *, sha: str) -> None:
        self._emit("vault.committed", sha=sha, sha_short=sha[:7])

    def log(self, message: str, *, level: EventLevel = "info", **payload: Any) -> None:
        """Generyczny log — wszystko co nie pasuje do dedykowanego helpera."""
        self._emit("log", level=level, message=message, **payload)

    # ------------------------------------------------------------------
    # context manager
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for sink in self._sinks:
            try:
                sink.close()
            except Exception as exc:
                _logger.error("Sink %s padl przy close: %r", sink.name, exc)

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()
