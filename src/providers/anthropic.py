"""Implementacja BaseProvider dla Anthropic Messages API przez httpx.

Mapuje ChatRequest / ChatMessage na payload POST /v1/messages oraz normalizuje
odpowiedz do ProviderResult, tak aby reszta aplikacji mogla latwo przelaczac
vendorow bez zmian w logice biznesowej.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Sequence

import httpx

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


class AnthropicProvider(BaseProvider):
    """Provider LLM dla Anthropic Messages API.

    System prompt jest wyciągany z wiadomości o roli ``system`` i przekazywany
    w top-level polu ``system`` zgodnie z wymaganiami Anthropic.
    """

    def __init__(
        self,
        *,
        api_key: str,
        default_model: str,
        base_url: str = "https://api.anthropic.com",
        anthropic_version: str = "2023-06-01",
        default_max_tokens: int = 4096,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        super().__init__(name="anthropic", default_model=default_model)
        self._base_url = base_url.rstrip("/")
        self._default_max_tokens = default_max_tokens
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={
                "x-api-key": api_key,
                "anthropic-version": anthropic_version,
                "content-type": "application/json",
            },
        )

    def _append_message(
        self,
        out: list[dict[str, Any]],
        *,
        role: str,
        blocks: list[dict[str, Any]],
    ) -> None:
        """Dodaje blok(i) content, scala kolejne wiadomości o tej samej roli."""
        if not blocks:
            return
        if out and out[-1].get("role") == role:
            existing = out[-1].get("content")
            if isinstance(existing, list):
                existing.extend(blocks)
                return
        out.append({"role": role, "content": blocks})

    def _map_tool_definitions(self, tools: Sequence[ToolDefinition]) -> list[dict[str, Any]]:
        """Mapuje wspólne definicje tooli na format Anthropic `tools`."""
        return [
            {
                "name": tool.function.name,
                "description": tool.function.description,
                "input_schema": tool.function.parameters or {"type": "object", "properties": {}},
            }
            for tool in tools
        ]

    def _map_tool_choice(self, request: ChatRequest) -> dict[str, Any] | None:
        """Mapuje wspólne `tool_choice` na format Anthropic."""
        if not request.tools:
            if request.tool_choice is not None or request.parallel_tool_calls is not None:
                raise ValueError(
                    "AnthropicProvider wymaga zdefiniowanych tools, "
                    "gdy ustawiasz tool_choice albo parallel_tool_calls"
                )
            return None

        mapped: dict[str, Any] | None = None
        choice = request.tool_choice

        if isinstance(choice, str):
            choice_type = "any" if choice == "required" else choice
            if choice_type not in {"auto", "any", "none"}:
                raise ValueError(
                    "AnthropicProvider obsluguje stringowe tool_choice tylko dla: "
                    "'auto', 'none', 'any', 'required'"
                )
            mapped = {"type": choice_type}
        elif isinstance(choice, dict):
            mapped = dict(choice)
            choice_type = mapped.get("type")
            if choice_type == "required":
                mapped["type"] = "any"
            if mapped.get("type") == "tool" and not isinstance(mapped.get("name"), str):
                raise ValueError("tool_choice typu 'tool' wymaga pola 'name'")
        elif choice is not None:
            raise ValueError("tool_choice musi byc stringiem, dict albo None")

        if request.parallel_tool_calls is not None:
            if mapped is None:
                mapped = {"type": "auto"}
            mapped["disable_parallel_tool_use"] = not request.parallel_tool_calls

        return mapped

    def _split_system_messages(
        self, messages: Sequence[ChatMessage]
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Rozdziela system prompt i mapuje wiadomości do Anthropic Messages API."""
        system_parts: list[str] = []
        out: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                system_parts.append(msg.content or "")
                continue

            if msg.role == MessageRole.TOOL:
                if not msg.tool_call_id:
                    raise ValueError(
                        "Wiadomosc z role=tool wymaga tool_call_id, "
                        "aby mozna bylo odeslac wynik do Anthropic"
                    )
                blocks = [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id,
                        "content": msg.content or "",
                    }
                ]
                self._append_message(out, role="user", blocks=blocks)
                continue

            role = "assistant" if msg.role == MessageRole.ASSISTANT else "user"
            blocks: list[dict[str, Any]] = []
            content = msg.content or ""

            if msg.role == MessageRole.USER and msg.name is not None:
                content = f"[{msg.name}] {content}"
            if content:
                blocks.append({"type": "text", "text": content})

            if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                for tool_call in msg.tool_calls:
                    if not tool_call.id:
                        raise ValueError(
                            "Wiadomosc assistant z tool_calls wymaga id dla kazdego wywolania, "
                            "aby Anthropic mogl sparowac pozniejsze tool_result"
                        )
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tool_call.id,
                            "name": tool_call.function.name,
                            "input": self._decode_tool_arguments(tool_call.function.arguments),
                        }
                    )

            self._append_message(out, role=role, blocks=blocks)

        system = "\n\n".join(part for part in system_parts if part).strip() or None
        return system, out

    def _decode_tool_arguments(self, raw: str) -> Any:
        """Zamienia JSON-string z ToolCall na obiekt oczekiwany przez Anthropic."""
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}

    def _build_payload(self, request: ChatRequest) -> dict[str, Any]:
        model = self._resolve_model(request)
        system, messages = self._split_system_messages(request.messages)
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": request.max_tokens or self._default_max_tokens,
        }

        if system is not None:
            payload["system"] = system
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop is not None:
            payload["stop_sequences"] = [request.stop] if isinstance(request.stop, str) else list(request.stop)
        if request.tools:
            payload["tools"] = self._map_tool_definitions(request.tools)

        tool_choice = self._map_tool_choice(request)
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

        payload.update(request.extra)
        return payload

    async def _post_with_retries(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.post("/v1/messages", json=payload)
                if resp.status_code in {429, 500, 529}:
                    if attempt >= self._max_retries:
                        break
                    retry_after = resp.headers.get("retry-after")
                    delay = float(retry_after) if retry_after else min(2**attempt, 8)
                    await asyncio.sleep(delay)
                    continue

                if resp.status_code >= 400:
                    detail = self._extract_error_message(resp)
                    raise RuntimeError(
                        f"Anthropic API zwrocilo {resp.status_code}: {detail}"
                    )

                return resp.json()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    break
                await asyncio.sleep(min(2**attempt, 8))

        if last_error is not None:
            raise RuntimeError(f"Blad komunikacji z Anthropic API: {last_error}") from last_error
        raise RuntimeError("Anthropic API chwilowo niedostepne po wyczerpaniu retry")

    def _extract_error_message(self, response: httpx.Response) -> str:
        try:
            data = response.json()
        except ValueError:
            return response.text

        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message

        return response.text

    def _extract_text(self, data: dict[str, Any]) -> str:
        content = data.get("content")
        if not isinstance(content, list):
            return ""

        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)

    def _extract_tool_calls(self, data: dict[str, Any]) -> list[ToolCall]:
        """Normalizuje bloki `tool_use` z odpowiedzi Anthropic."""
        content = data.get("content")
        if not isinstance(content, list):
            return []

        out: list[ToolCall] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue

            name = block.get("name")
            if not isinstance(name, str) or not name:
                continue

            tool_input = block.get("input")
            out.append(
                ToolCall(
                    id=block.get("id") if isinstance(block.get("id"), str) else None,
                    type="function",
                    function=ToolFunctionCall(
                        name=name,
                        arguments=json.dumps(tool_input if tool_input is not None else {}),
                    ),
                )
            )

        return out

    async def complete(self, request: ChatRequest) -> ProviderResult:
        """Wykonuje zapytanie do Anthropic Messages API i zwraca ProviderResult."""
        payload = self._build_payload(request)
        data = await self._post_with_retries(payload)

        usage_data = data.get("usage")
        usage = None
        if isinstance(usage_data, dict):
            input_tokens = usage_data.get("input_tokens")
            output_tokens = usage_data.get("output_tokens")
            total_tokens = None
            if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                total_tokens = input_tokens + output_tokens
            usage = UsageStats(
                input_tokens=input_tokens if isinstance(input_tokens, int) else None,
                output_tokens=output_tokens if isinstance(output_tokens, int) else None,
                total_tokens=total_tokens,
            )

        return ProviderResult(
            text=self._extract_text(data),
            model=str(data.get("model") or payload["model"]),
            finish_reason=data.get("stop_reason")
            if isinstance(data.get("stop_reason"), str)
            else None,
            usage=usage,
            tool_calls=self._extract_tool_calls(data),
            raw=data,
        )
