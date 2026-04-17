"""ContextVar do przekazywania metadanych wywolania LLM do loggera.

Agent ustawia kontekst przed ``await provider.complete(...)``, wrapper
``LoggingProvider`` odczytuje i doklada do eventu. Dzieki temu
``ChatRequest`` pozostaje czysty (nic o logowaniu), a wrapper nie musi
zgadywac co to za wywolanie.

Kontekst jest tylko do metadanych semantycznych (faza agenta, chunk).
Tokeny/latency/model mierzy sam wrapper.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class LLMCallContext:
    """Semantyczny opis JEDNEGO wywolania LLM z perspektywy agenta.

    ``phase`` — ``"SMALL"`` (jeden request na maly commit),
    ``"CHUNK_SUMMARY"`` (multi-turn, tura per chunk), ``"FINALIZE"``
    (zebrane podsumowania -> submit_plan).

    ``attempt`` — numer proby (1-based), bo ``propose_actions`` ma retry
    walidacji. Dzieki temu w logach widac retry po walidacji Pydantic.
    """

    phase: str
    commit_sha: str | None = None
    chunk_idx: int | None = None
    chunk_total: int | None = None
    chunk_id: str | None = None
    attempt: int | None = None
    files: tuple[str, ...] = ()


_current: ContextVar[LLMCallContext | None] = ContextVar(
    "obsagent_llm_call_context", default=None
)


def get_current_llm_context() -> LLMCallContext | None:
    """Zwraca kontekst ustawiony przez ``llm_call_context`` albo None."""
    return _current.get()


@contextlib.contextmanager
def llm_call_context(ctx: LLMCallContext) -> Iterator[LLMCallContext]:
    """Ustawia ``LLMCallContext`` na czas bloku (async-safe przez ContextVar).

    Uzycie w ``agent.py``::

        with llm_call_context(LLMCallContext(phase="FINALIZE", ...)):
            result = await self.provider.complete(request)
    """
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)
