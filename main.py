"""Szybki test: fabryka → OpenAIProvider → jedno ``complete``."""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.providers.base import ChatMessage, ChatRequest, MessageRole
from src.providers.factory import build_provider


async def main() -> None:
    cfg = Path(__file__).resolve().parent / "config.yaml"
    provider = build_provider(cfg)
    request = ChatRequest(
        messages=[
            ChatMessage(
                role=MessageRole.USER,
                content="Odpowiedz jednym krótkim zdaniem: czy test działa?",
            ),
        ],
    )
    result = await provider.complete(request)
    print(result.text)
    print("model:", result.model)


if __name__ == "__main__":
    asyncio.run(main())
