"""``InMemoryMcpTransport`` - klient MCP bez HTTP dla testow (Faza 7).

**Rola:** zastapuje ``McpAgentClient`` w testach jednostkowych gdzie nie
chcemy podnosic serwera ``FastMCP`` / portu localhost. Implementuje ten
sam publiczny kontrakt (``connected``, ``list_tools``, ``call_tool``,
``connect``, ``close``, ``ensure_connected``, ``url``), ale dispatchuje
wywolania **bezposrednio do** ``ToolRegistry.dispatch`` - bez
serializacji HTTP, bez SSE, bez ``streamable-http``.

Po fazie 7 ``mcp.enabled=false`` zostalo usuniete z ``Agent.start_mcp``:
MCP to jedyna sciezka dispatchu narzedzi w produkcji. Testy jednak nie
moga zalezec od wolnego portu HTTP (kolizje na CI, opoznienie ~100ms na
start serwera), wiec ``InMemoryMcpTransport`` daje ten sam interfejs
przy zero koszcie sieciowym.

**Uzycie:**

.. code-block:: python

    agent = Agent(...)  # rejestruje tools w _register_default_tools
    transport = InMemoryMcpTransport(
        registry=agent.tool_registry,
        ctx_provider=agent._tool_ctx_provider,
    )
    agent.attach_mcp_client(transport)  # pomija start_mcp()
    await agent.run_session(commit_info)

**Co NIE robi transport:**

- Nie uruchamia ``FastMCP`` - zadne ``FastMCPTool`` nie jest budowane.
- Nie cachuje wynikow ``call_tool`` - kazde wywolanie idzie do registry.
- Nie implementuje MCP protocol errors - jesli ``tool.execute`` rzuci,
  ``ToolRegistry.dispatch`` lapie i mapuje na ``ToolResult(ok=False)``.

**Co robi identycznie jak ``McpAgentClient``:**

- Zwraca ``ToolDefinition`` z polami (``type``, ``function.name``,
  ``function.description``, ``function.parameters``) - format zrozumialy
  dla providerow (OpenAI / Anthropic / OpenRouter).
- Cache'uje ``list_tools`` - pierwsza inwokacja buduje definicje, kolejne
  zwracaja ta sama referencje (prompt caching friendly w testach).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from src.agent.tools.base import ToolResult
from src.providers.base import (
    ToolCall,
    ToolDefinition,
    ToolFunctionCall,
    ToolFunctionDefinition,
)

if TYPE_CHECKING:
    from src.agent.mcp.adapter import CtxProvider
    from src.agent.tools.registry import ToolRegistry


class InMemoryMcpTransport:
    """Minimalny in-memory stub zgodny z publicznym API ``McpAgentClient``.

    Duck-typing: agent trzyma ``_mcp_client: McpAgentClient | None``, ale
    uzywa wylacznie metod ``connected`` / ``list_tools`` / ``call_tool`` /
    ``ensure_connected`` / ``close``. Ta klasa wystawia je tak samo,
    wiec agent nie musi o niej nic wiedziec poza tym ze dostal juz
    "polaczony" transport.
    """

    def __init__(
        self,
        *,
        registry: "ToolRegistry",
        ctx_provider: "CtxProvider",
    ) -> None:
        self._registry = registry
        self._ctx_provider = ctx_provider
        self._connected = False
        self._cached_tools: list[ToolDefinition] | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def url(self) -> str:
        return "in-memory://mcp"

    async def connect(self) -> None:
        """Oznacza transport jako gotowy. Idempotentne."""

        self._connected = True

    async def ensure_connected(self) -> None:
        await self.connect()

    async def close(self) -> None:
        """Zamyka transport. Idempotentne."""

        self._connected = False
        self._cached_tools = None

    async def __aenter__(self) -> "InMemoryMcpTransport":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def list_tools(self) -> list[ToolDefinition]:
        """Zwraca ``ToolDefinition`` per narzedzie w registry (cache).

        Kolejnosc = ``registry.names()`` (alfabet) dla determinizmu
        identycznie jak w ``mount_registry_on_mcp``.
        """

        if not self._connected:
            raise RuntimeError(
                "InMemoryMcpTransport nie jest polaczony - zawolaj connect()."
            )
        if self._cached_tools is not None:
            return self._cached_tools

        definitions: list[ToolDefinition] = []
        for name in self._registry.names():
            tool = self._registry.get(name)
            if tool is None:
                continue
            definitions.append(
                ToolDefinition(
                    type="function",
                    function=ToolFunctionDefinition(
                        name=tool.name,
                        description=tool.description or "",
                        parameters=dict(tool.input_schema() or {}),
                    ),
                )
            )
        self._cached_tools = definitions
        return definitions

    async def refresh_tools(self) -> list[ToolDefinition]:
        """Zrzuca cache i rebuilduje z registry - tylko dla testow z hot-reload."""

        self._cached_tools = None
        return await self.list_tools()

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Dispatchuje ``name(args)`` przez ``ToolRegistry`` bez HTTP.

        Bledy nie propagujace sie na zewnatrz - wszystko jest lapane
        w ``ToolRegistry.dispatch`` i mapowane na ``ToolResult(ok=False)``,
        tak samo jak po stronie ``McpAgentClient``.
        """

        if not self._connected:
            raise RuntimeError(
                "InMemoryMcpTransport nie jest polaczony - zawolaj connect()."
            )

        ctx = self._ctx_provider()
        tool_call = ToolCall(
            id=None,
            type="function",
            function=ToolFunctionCall(
                name=name,
                arguments=json.dumps(args or {}, ensure_ascii=False),
            ),
        )
        return await self._registry.dispatch(tool_call, ctx)


__all__ = ["InMemoryMcpTransport"]
