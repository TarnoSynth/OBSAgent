"""LoggingProvider — wrapper na BaseProvider dodajacy strukturalne logowanie.

Decorator pattern: ``LoggingProvider(inner, run_logger)`` implementuje
ten sam interfejs ``BaseProvider``, deleguje ``complete()`` do wewnetrznego,
ale owija kazde wywolanie w ``try/except`` ze stopwatchem i zapisem
metadanych z ``LLMCallContext`` (ContextVar) + usage/tokenow z odpowiedzi.

Dzieki temu reszta aplikacji nie wie, ze istnieje logger — w ``Agent``
mamy dalej ``self.provider.complete(request)``, a wrapper robi robote.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from src.providers.base import BaseProvider, ChatRequest, ProviderResult

from logs.context import get_current_llm_context
from logs.run_logger import RunLogger


def _truncate(text: str, limit: int = 400) -> str:
    """Pierwsze ``limit`` znakow + suffix, zeby JSONL nie urosl na bledach."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...(+{len(text) - limit} chars)"


class LoggingProvider(BaseProvider):
    """Wrapper na dowolny BaseProvider ze strukturalnym logowaniem LLM calls."""

    def __init__(self, inner: BaseProvider, run_logger: RunLogger) -> None:
        super().__init__(name=inner.name, default_model=inner.default_model)
        self._inner = inner
        self._rl = run_logger

    def _base_payload(self, request: ChatRequest) -> dict[str, Any]:
        ctx = get_current_llm_context()
        payload: dict[str, Any] = {
            "provider": self._inner.name,
            "model": self._resolve_model(request),
        }
        if ctx is not None:
            payload["phase"] = ctx.phase
            if ctx.commit_sha is not None:
                payload["commit_sha"] = ctx.commit_sha
                payload["commit_sha_short"] = ctx.commit_sha[:7]
            if ctx.chunk_idx is not None:
                payload["chunk_idx"] = ctx.chunk_idx
            if ctx.chunk_total is not None:
                payload["chunk_total"] = ctx.chunk_total
            if ctx.chunk_id is not None:
                payload["chunk_id"] = ctx.chunk_id
            if ctx.attempt is not None:
                payload["attempt"] = ctx.attempt
            if ctx.files:
                payload["files"] = list(ctx.files)
        else:
            payload["phase"] = "UNKNOWN"
        return payload

    async def complete(self, request: ChatRequest) -> ProviderResult:
        payload = self._base_payload(request)
        payload["messages_count"] = len(request.messages)
        payload["tools_count"] = len(request.tools)
        payload["tool_choice"] = (
            request.tool_choice if isinstance(request.tool_choice, str) else
            ("dict" if request.tool_choice else None)
        )
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        self._rl.log_llm_started(**payload)

        t0 = time.monotonic()
        try:
            result = await self._inner.complete(request)
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            self._rl.log_llm_failed(
                **payload,
                latency_ms=latency_ms,
                error_type=type(exc).__name__,
                error_message=_error_message(exc),
                error_repr=repr(exc),
                http_status=_extract_http_status(exc),
                response_body_snippet=_extract_body_snippet(exc),
            )
            raise

        latency_ms = int((time.monotonic() - t0) * 1000)
        usage = result.usage
        ok_payload = {
            **payload,
            "latency_ms": latency_ms,
            "finish_reason": result.finish_reason,
            "tool_calls_count": len(result.tool_calls),
            "text_length": len(result.text or ""),
        }
        if usage is not None:
            ok_payload["input_tokens"] = usage.input_tokens
            ok_payload["output_tokens"] = usage.output_tokens
            ok_payload["total_tokens"] = usage.total_tokens
            # Anthropic prompt caching — None dla OpenAI/OpenRouter.
            # Dzieki temu w jsonl widac od razu czy cache dziala (cache_read > 0).
            if usage.cache_creation_input_tokens is not None:
                ok_payload["cache_creation_input_tokens"] = usage.cache_creation_input_tokens
            if usage.cache_read_input_tokens is not None:
                ok_payload["cache_read_input_tokens"] = usage.cache_read_input_tokens

        # Model uzyty faktycznie (moze byc inny niz resolved przy fallbacku).
        if result.model and result.model != payload.get("model"):
            ok_payload["model_returned"] = result.model

        self._rl.log_llm_ok(**ok_payload)
        return result


def _error_message(exc: BaseException) -> str:
    """Wyciaga sensowny komunikat — niepusty nawet dla ReadTimeout itd."""
    msg = str(exc)
    if msg.strip():
        return msg
    return f"{type(exc).__name__}()"


def _extract_http_status(exc: BaseException) -> int | None:
    """Jesli wyjatek niesie status HTTP (httpx / wrapper RuntimeError), wyciagnij."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return _extract_http_status(cause)
    return None


def _extract_body_snippet(exc: BaseException) -> str | None:
    """Jesli wyjatek niesie response text, wez pierwsze 400 znakow."""
    response = getattr(exc, "response", None)
    if response is not None:
        text = getattr(response, "text", None)
        if isinstance(text, str) and text:
            return _truncate(text)
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return _extract_body_snippet(cause)
    return None


# Re-export ``asdict`` miejsce uzycia wyzej — silencer dla ruff / mypy.
_ = asdict
