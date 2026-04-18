"""``build_mcp_server`` - factory dla lokalnego serwera FastMCP (Faza 1).

Serwer jest **cienką warstwą nad** ``ToolRegistry``:

1. Tworzymy ``FastMCP(name, instructions, host, port, streamable_http_path)``.
2. Wołamy ``mount_registry_on_mcp(mcp, registry, ctx_provider)`` - wszystkie
   narzędzia z registry lecą 1:1 na MCP (te same nazwy, descriptions, schematy).
3. Zwracamy obiekt ``FastMCP`` gotowy do ``run_streamable_http_async()``.

Uruchamianiem rządzi ``McpRuntime`` (``runtime.py``) - on owija serwer
w ``asyncio.Task`` i dba o graceful shutdown. Tu zostawiamy samą konstrukcję
- testy mogą zbudować serwer, pogrzebać w jego stanie, i nie startować HTTP.

**Konwencja ``instructions``:** krótki opis dla klientów MCP widzących
serwer (np. Claude Desktop). Informuje że to **in-process** runtime agenta
dokumentacji, nie publiczny endpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

from src.agent.mcp.adapter import CtxProvider, mount_registry_on_mcp
from src.agent.mcp.config import MCP_HTTP_PATH, McpSettings

if TYPE_CHECKING:
    from src.agent.tools.registry import ToolRegistry


_DEFAULT_INSTRUCTIONS = (
    "ObsAgent - lokalny serwer MCP agenta dokumentacji (in-process runtime). "
    "Udostępnia narzędzia do eksploracji i modyfikacji vaulta Obsidian zsynchronizowanego "
    "z repo projektu. Wszystkie wywołania idą przez ToolRegistry agenta - zewnętrzny "
    "klient MCP widzi dokładnie te same narzędzia, które widzi pętla tool-use modelu LLM. "
    "Zapisy do vaulta przechodzą przez pending batch agenta (preview + user confirm) - "
    "bezpośredni call z klienta MCP NIE commituje plików do Git."
)


def build_mcp_server(
    registry: "ToolRegistry",
    ctx_provider: CtxProvider,
    settings: McpSettings,
) -> FastMCP:
    """Buduje i zwraca skonfigurowany ``FastMCP`` (nie startuje go).

    :param registry: źródło prawdy dla narzędzi - wszystko z niego jest
        propagowane 1:1 do MCP.
    :param ctx_provider: getter ``ToolExecutionContext``-u per wywołanie.
        Agent ustawia świeży context per commit, MCP server go nie zna
        bezpośrednio - bierze przez ten getter.
    :param settings: host/port/name/transport z ``config.yaml``.

    :return: gotowa instancja ``FastMCP`` z zamontowanymi narzędziami.
        Start: ``await mcp.run_streamable_http_async()`` (wewnątrz
        ``McpRuntime``).
    """

    mcp = FastMCP(
        name=settings.server_name,
        instructions=_DEFAULT_INSTRUCTIONS,
        host=settings.host,
        port=settings.port,
        streamable_http_path=MCP_HTTP_PATH,
        log_level="WARNING",
        warn_on_duplicate_tools=False,
    )

    mount_registry_on_mcp(mcp, registry, ctx_provider)
    return mcp


__all__ = ["build_mcp_server"]
