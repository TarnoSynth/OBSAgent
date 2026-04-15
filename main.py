"""Krótki przykład użycia providerów z tool callingiem.

Uruchom:
    python main.py

Przed uruchomieniem ustaw w `config.yaml` wybranego providera, np. `openai`,
oraz odpowiedni klucz API w `.env`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from src.providers.base import (
    BaseProvider,
    ChatMessage,
    ChatRequest,
    MessageRole,
    ToolDefinition,
    ToolFunctionDefinition,
)
from src.providers.factory import build_provider


def _tool_echo(*, text: str) -> str:
    return text


def _safe_json_loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


async def _run_tool_demo(provider: BaseProvider) -> None:
    """Minimalny przepływ: assistant -> tool -> assistant."""
    # Minimalne narzedzie do testu E2E tool callingu.
    tools = [
        ToolDefinition(
            function=ToolFunctionDefinition(
                name="echo",
                description="Zwraca przekazany tekst (narzedzie testowe).",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            )
        )
    ]

    print("provider:", provider.name)
    messages: list[ChatMessage] = [
        ChatMessage(
            role=MessageRole.USER,
            content=(
                "Uzyj narzedzia echo z argumentem text='hello from tool'. "
                "Potem odpowiedz jednym zdaniem, co zwrocilo narzedzie."
            ),
        )
    ]

    # 1) Prosba o tool calls
    request = ChatRequest(
        messages=messages,
        tools=tools,
        tool_choice="auto",
        parallel_tool_calls=False,
    )
    result = await provider.complete(request)

    print("model:", result.model)
    if result.tool_calls:
        print("tool_calls:", [tc.function.name for tc in result.tool_calls])
    if result.text:
        print("assistant_text(pre):", result.text)

    # 2) Wykonanie narzedzi + odeslanie wynikow jako role=tool
    if result.tool_calls:
        messages.append(
            ChatMessage(
                role=MessageRole.ASSISTANT,
                content=result.text or None,
                tool_calls=result.tool_calls,
            )
        )

        for tc in result.tool_calls:
            args = _safe_json_loads(tc.function.arguments)
            if tc.function.name == "echo":
                text = args.get("text", "")
                tool_out = _tool_echo(text=str(text))
            else:
                tool_out = f"Nieznane narzedzie: {tc.function.name}"

            messages.append(
                ChatMessage(
                    role=MessageRole.TOOL,
                    tool_call_id=tc.id,
                    content=tool_out,
                )
            )

        # 3) Finalna odpowiedz po wynikach tooli
        followup = await provider.complete(
            ChatRequest(messages=messages, tools=tools, tool_choice="auto")
        )
        print("assistant_text(final):", followup.text)
        print("finish_reason:", followup.finish_reason)
        print("model(final):", followup.model)
    else:
        print("assistant_text:", result.text)
        print("finish_reason:", result.finish_reason)


async def main() -> None:
    cfg = Path(__file__).resolve().parent / "config.yaml"
    provider = build_provider(cfg)
    await _run_tool_demo(provider)


if __name__ == "__main__":
    asyncio.run(main())
