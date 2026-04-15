"""Implementacja BaseProvider dla oficjalnego klienta OpenAI (AsyncOpenAI).

Mapuje ChatRequest / ChatMessage na wywołanie ``chat.completions.create`` i
normalizuje odpowiedz do ProviderResult, razem z obsluga tool callingu.
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
    ToolCall,
    ToolDefinition,
    ToolFunctionCall,
    UsageStats,
)


class OpenAIProvider(BaseProvider):
    """Provider LLM przez AsyncOpenAI (endpoint Chat Completions).

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

    @staticmethod
    def _get_field(obj: Any, name: str) -> Any:
        """Czyta pole z obiektu SDK albo slownika."""
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    def _map_tool_definitions(self, tools: Sequence[ToolDefinition]) -> list[dict[str, Any]]:
        """Mapuje wspolne definicje narzedzi na format `tools` dla OpenAI."""
        return [
            {
                "type": tool.type,
                "function": {
                    "name": tool.function.name,
                    "description": tool.function.description,
                    "parameters": tool.function.parameters,
                },
            }
            for tool in tools
        ]

    def _map_tool_calls(self, tool_calls: Sequence[ToolCall]) -> list[dict[str, Any]]:
        """Mapuje assistant.tool_calls na format oczekiwany przez OpenAI."""
        return [
            {
                "id": tool_call.id,
                "type": tool_call.type,
                "function": {
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments,
                },
            }
            for tool_call in tool_calls
        ]

    def _extract_tool_calls(self, raw_tool_calls: Any) -> list[ToolCall]:
        """Normalizuje tool_calls zwrocone przez OpenAI SDK."""
        if not raw_tool_calls:
            return []

        out: list[ToolCall] = []
        for raw_tool_call in raw_tool_calls:
            function = self._get_field(raw_tool_call, "function")
            name = self._get_field(function, "name")
            arguments = self._get_field(function, "arguments")
            if not isinstance(name, str) or not name:
                continue
            out.append(
                ToolCall(
                    id=self._get_field(raw_tool_call, "id"),
                    type=str(self._get_field(raw_tool_call, "type") or "function"),
                    function=ToolFunctionCall(
                        name=name,
                        arguments=arguments if isinstance(arguments, str) else "",
                    ),
                )
            )
        return out

    def _map_messages(self, messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
        """Mapuje wspólne ChatMessage na `messages` dla OpenAI Chat Completions."""
        out: list[dict[str, Any]] = []

        for msg in messages:
            item: dict[str, Any] = {"role": msg.role.value}
            if msg.content is not None:
                item["content"] = msg.content
            elif msg.role != MessageRole.ASSISTANT or not msg.tool_calls:
                item["content"] = ""

            if msg.name is not None and msg.role == MessageRole.USER:
                item["name"] = msg.name
            if msg.role == MessageRole.TOOL:
                if not msg.tool_call_id:
                    raise ValueError(
                        "Wiadomosc z role=tool wymaga tool_call_id, "
                        "aby mozna bylo poprawnie odeslac wynik narzedzia do OpenAI"
                    )
                item["tool_call_id"] = msg.tool_call_id
            if msg.tool_calls:
                item["tool_calls"] = self._map_tool_calls(msg.tool_calls)

            out.append(item)
        return out

    async def complete(self, request: ChatRequest) -> ProviderResult:
        """Wykonuje `chat.completions.create` i zwraca ProviderResult z tool_callami."""
        if request.extra.get("stream") is True:
            raise ValueError(
                "OpenAIProvider.complete() obsluguje tylko non-streaming; "
                "ustaw stream=False lub dodaj osobny interfejs streamingowy"
            )

        model = self._resolve_model(request)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._map_messages(request.messages),
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p
        if request.stop is not None:
            kwargs["stop"] = request.stop
        if request.tools:
            kwargs["tools"] = self._map_tool_definitions(request.tools)
        if request.tool_choice is not None:
            kwargs["tool_choice"] = request.tool_choice
        if request.parallel_tool_calls is not None:
            kwargs["parallel_tool_calls"] = request.parallel_tool_calls
        if request.extra:
            kwargs["extra_body"] = request.extra

        resp = await self._client.chat.completions.create(**kwargs)

        usage = None
        if resp.usage is not None:
            usage = UsageStats(
                input_tokens=resp.usage.prompt_tokens,
                output_tokens=resp.usage.completion_tokens,
                total_tokens=resp.usage.total_tokens,
            )

        first_choice = resp.choices[0] if resp.choices else None
        text = ""
        finish_reason = None
        tool_calls = []
        if first_choice is not None:
            text = first_choice.message.content or ""
            finish_reason = (
                str(first_choice.finish_reason)
                if first_choice.finish_reason is not None
                else None
            )
            tool_calls = self._extract_tool_calls(first_choice.message.tool_calls)

        return ProviderResult(
            text=text,
            model=resp.model or model,
            finish_reason=finish_reason,
            usage=usage,
            tool_calls=tool_calls,
            raw=resp,
        )
