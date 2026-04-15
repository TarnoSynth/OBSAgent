"""Implementacja BaseProvider dla OpenRouter Chat Completions przez AsyncOpenAI.

OpenRouter utrzymuje schemat zblizony do OpenAI Chat API, wiec adapter moze
wykorzystac oficjalne SDK `openai` po ustawieniu `base_url`.
Fallback modeli (`models`) i routing providerow (`provider`) przekazujemy przez
`request.extra`, co pozwala sterowac zachowaniem per-request bez zaszywania
szczegolow OpenRoutera w warstwie biznesowej.
"""

from __future__ import annotations

from typing import Any, Sequence

from openai import APIError, AsyncOpenAI

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


class OpenRouterProvider(BaseProvider):
    """Provider LLM dla OpenRouter `POST /chat/completions`.

    Adapter obsluguje tylko tryb non-streaming, bo wspolny kontrakt `complete()`
    zwraca gotowy `ProviderResult`. Wszystkie specyficzne dla OpenRouter pola
    (np. `models`, `provider`, `response_format`, `tools`) nalezy przekazywac
    przez `ChatRequest.extra`.
    """

    def __init__(
        self,
        *,
        api_key: str,
        default_model: str,
        base_url: str = "https://openrouter.ai/api/v1",
        http_referer: str | None = None,
        app_title: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(name="openrouter", default_model=default_model)
        default_headers: dict[str, str] = {}
        if http_referer:
            default_headers["HTTP-Referer"] = http_referer
        if app_title:
            default_headers["X-OpenRouter-Title"] = app_title

        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            default_headers=default_headers or None,
        )

    def _map_tool_definitions(self, tools: Sequence[ToolDefinition]) -> list[dict[str, Any]]:
        """Mapuje wspolne definicje narzedzi na format `tools` dla Chat Completions."""
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
        """Mapuje wywolania narzedzi assistant -> `tool_calls` dla Chat Completions."""
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
        """Normalizuje `message.tool_calls` z odpowiedzi SDK do wspolnego ToolCall."""
        if not raw_tool_calls:
            return []

        out: list[ToolCall] = []
        for raw_tool_call in raw_tool_calls:
            function = getattr(raw_tool_call, "function", None)
            name = getattr(function, "name", None)
            arguments = getattr(function, "arguments", None)
            if not isinstance(name, str):
                continue
            out.append(
                ToolCall(
                    id=getattr(raw_tool_call, "id", None),
                    type=str(getattr(raw_tool_call, "type", "function")),
                    function=ToolFunctionCall(
                        name=name,
                        arguments=arguments if isinstance(arguments, str) else "",
                    ),
                )
            )
        return out

    def _uses_tool_calling(self, request: ChatRequest) -> bool:
        """Sprawdza, czy request korzysta z mechaniki tool callingu."""
        if request.tools or request.tool_choice is not None or request.parallel_tool_calls is not None:
            return True
        return any(msg.tool_calls or msg.role == MessageRole.TOOL for msg in request.messages)

    def _extract_api_error_message(self, exc: APIError) -> str:
        """Wyciaga czytelny tekst bledu z OpenAI-compatible SDK."""
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()
            message = body.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return str(exc).strip()

    def _raise_tool_calling_unsupported(self, request: ChatRequest, model: str, exc: APIError) -> None:
        """Zamienia blad API na czytelny komunikat o braku wsparcia dla narzedzi."""
        message = self._extract_api_error_message(exc)
        lowered = message.lower()
        if self._uses_tool_calling(request) and any(
            phrase in lowered
            for phrase in (
                "tool",
                "function call",
                "tool_choice",
                "parallel_tool_calls",
                "unsupported parameter",
                "not supported",
                "does not support",
            )
        ):
            raise RuntimeError(
                f"Model '{model}' w OpenRouter nie obsluguje tool callingu albo "
                f"udostepnionych parametrow narzedzi. Szczegoly API: {message}"
            ) from exc
        raise RuntimeError(f"Blad OpenRouter API dla modelu '{model}': {message}") from exc

    def _map_messages(self, messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
        """Mapuje wspolne ChatMessage na `messages` dla Chat Completions."""
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
                        "aby mozna bylo poprawnie odeslac wynik narzedzia do OpenRouter"
                    )
                item["tool_call_id"] = msg.tool_call_id
            if msg.tool_calls:
                item["tool_calls"] = self._map_tool_calls(msg.tool_calls)
            out.append(item)

        return out

    async def complete(self, request: ChatRequest) -> ProviderResult:
        """Wykonuje `chat.completions.create` na OpenRouter i normalizuje wynik."""
        if request.extra.get("stream") is True:
            raise ValueError(
                "OpenRouterProvider.complete() obsluguje tylko non-streaming; "
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
            kwargs["max_completion_tokens"] = request.max_tokens
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

        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except APIError as exc:
            self._raise_tool_calling_unsupported(request, model, exc)

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
        tool_calls: list[ToolCall] = []
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
