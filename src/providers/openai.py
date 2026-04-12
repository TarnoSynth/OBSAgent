"""Implementacja BaseProvider dla oficjalnego klienta OpenAI (AsyncOpenAI).

Mapuje ChatRequest / ChatMessage na wywołanie ``responses.create`` i odpowiedź → ProviderResult.
"""

from __future__ import annotations

from typing import Any, Sequence

from openai import AsyncOpenAI

from .base import (
    BaseProvider,
    ChatMessage,
    ChatRequest,
    MessageRole,
    ProviderResult,
    UsageStats,
)


class OpenAIProvider(BaseProvider):
    """Provider LLM przez AsyncOpenAI (endpoint Responses).

    Sekrety: tylko api_key (z .env), nie w kodzie. base_url tylko gdy inny host niż domyślny
    (proxy, Azure, gateway wewnętrzny).
    """

    def __init__(
        self,
        *,
        api_key: str,
        default_model: str,
        base_url: str | None = None,
    ) -> None:
        super().__init__(name="openai", default_model=default_model)
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    def _map_response_input(self, messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
        """Buduje ``input`` dla Responses: lista elementów ``type: message`` + ``role`` + ``content``."""
        out: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == MessageRole.TOOL:
                out.append(
                    {
                        "type": "message",
                        "role": "user",
                        "content": f"[tool:{msg.name or '?'}]\n{msg.content}",
                    }
                )
                continue
            content = msg.content
            if msg.name is not None and msg.role == MessageRole.USER:
                content = f"[{msg.name}] {content}"
            out.append(
                {
                    "type": "message",
                    "role": msg.role.value,
                    "content": content,
                }
            )
        return out

    async def complete(self, request: ChatRequest) -> ProviderResult:
        """Wykonuje ``responses.create``; opcjonalne pola z ChatRequest tylko jeśli != None.

        ``request.extra`` doklejane na końcu — nadpisuje wcześniejsze klucze o tej samej nazwie.
        ``stop`` nie mapujemy domyślnie (Responses ma inne przełączniki); możesz podać w ``extra``.
        """
        model = self._resolve_model(request)
        kwargs: dict[str, Any] = {
            "model": model,
            "input": self._map_response_input(request.messages),
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            kwargs["max_output_tokens"] = request.max_tokens
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p
        kwargs.update(request.extra)

        resp = await self._client.responses.create(**kwargs)

        text = resp.output_text or ""
        usage = None
        if resp.usage is not None:
            usage = UsageStats(
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                total_tokens=resp.usage.total_tokens,
            )

        return ProviderResult(
            text=text,
            model=resp.model or model,
            finish_reason=str(resp.status) if resp.status is not None else None,
            usage=usage,
            raw=resp,
        )
