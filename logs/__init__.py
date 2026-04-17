"""Publiczne API modulu logow agenta.

Architektura (dwie warstwy):

1. **Stdlib logging** (``configure_stdlib_logging``) — klasyczne logi
   tekstowe do pliku ``logs/runs/stdlib.log`` i na stderr (WARNING+).
   Lapie cos co zrobi ``logger = logging.getLogger(__name__)`` w ``src/*``.

2. **Structured events** (``RunLogger``) — typowane eventy biegu agenta
   (``llm.call.started``, ``commit.processed``, ``action.failed``, ...).
   Wysylane do sinkow: JSONL (domyslny) + Console (bledy + banner).

Uzycie z ``main.py``::

    from logs import RunLogger, configure_stdlib_logging
    from logs.context import LLMCallContext, llm_call_context

    configure_stdlib_logging(level=logging.INFO, log_dir=Path("logs/runs"))
    with RunLogger.create(log_dir=Path("logs/runs"), project_name="ObsAgent") as rl:
        rl.log_run_started(provider="anthropic", model="claude-opus-4-7", ...)
        ...

Uzycie z ``Agent`` (pod kapota main.py owija providera)::

    from logs.provider_logger import LoggingProvider
    provider = LoggingProvider(raw_provider, run_logger)

I wokol kazdego wywolania LLM::

    with llm_call_context(LLMCallContext(phase="FINALIZE", commit_sha=sha, ...)):
        result = await self.provider.complete(request)
"""

from logs.context import LLMCallContext, get_current_llm_context, llm_call_context
from logs.events import EVENT_TYPES, Event, EventLevel, utcnow
from logs.provider_logger import LoggingProvider
from logs.run_logger import RunLogger
from logs.setup import configure_stdlib_logging
from logs.sinks import ConsoleSink, JsonlSink, Sink

__all__ = [
    "EVENT_TYPES",
    "ConsoleSink",
    "Event",
    "EventLevel",
    "JsonlSink",
    "LLMCallContext",
    "LoggingProvider",
    "RunLogger",
    "Sink",
    "configure_stdlib_logging",
    "get_current_llm_context",
    "llm_call_context",
    "utcnow",
]
