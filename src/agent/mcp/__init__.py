"""Publiczne API warstwy MCP agenta (Faza 1 refaktoru agentic tool loop).

**Rola:** lokalny (in-process) serwer Model Context Protocol wystawiający
narzędzia z ``ToolRegistry`` przez transport streamable-http. Agent łączy
się do tego serwera klientem MCP i woła narzędzia przez standardowy
protokół - dzięki temu:

1. Definicje narzędzi są jednorazowo pobrane (``list_tools``) i cachowane
   po stronie klienta → Anthropic prompt caching widzi stabilną listę.
2. Ten sam serwer można podpiąć do zewnętrznych klientów MCP (Claude Desktop,
   Cursor IDE, mcp-inspector) kiedy agent działa.
3. Native ``Tool.execute`` (Faza 0) nie wie o MCP - MCP jest cienką warstwą
   nad ``ToolRegistry.dispatch``.

**Eksporty:**

- ``McpSettings``       - dataclass z konfiguracją (host/port/transport)
- ``build_mcp_server``  - factory ``FastMCP`` dla ``ToolRegistry``
- ``McpRuntime``        - lifecycle (start/stop) serwera w asyncio.Task
- ``McpAgentClient``    - klient MCP wolany przez Agenta w pętli tool-use
- ``mount_registry_on_mcp`` - adapter ``ToolRegistry → FastMCP`` (pomocny
  dla testów i zaawansowanych konfiguracji)

**Architektura in-process:**

Serwer MCP i klient żyją w tym samym procesie Python i tym samym event
loopie. Komunikują się przez localhost HTTP (streamable-http transport
= POST /mcp + SSE dla streamingu). Brak osobnego procesu - ale pełen
protokół MCP działa i klient zewnętrzny (inny proces) też może się
podłączyć do ``http://127.0.0.1:8765/mcp`` kiedy agent biegnie.

Przepływ sterowania::

    ┌──────────────┐ call_tool    ┌──────────────┐ HTTP/SSE ┌──────────────┐
    │    Agent     │─────────────▶│ McpAgentClient│─────────▶│  FastMCP     │
    │ (agent.py)   │              │  (client.py) │          │  (server.py) │
    └──────────────┘              └──────────────┘          └──────┬───────┘
                                                                   │
                                                                   ▼
                                                        ┌──────────────────┐
                                                        │  ToolRegistry    │
                                                        │  (tools/registry)│
                                                        └──────────────────┘
"""

from src.agent.mcp.adapter import CtxProvider, mount_registry_on_mcp
from src.agent.mcp.client import DEFAULT_CALL_TIMEOUT_S, McpAgentClient
from src.agent.mcp.config import MCP_HTTP_PATH, McpSettings
from src.agent.mcp.in_memory import InMemoryMcpTransport
from src.agent.mcp.runtime import McpRuntime
from src.agent.mcp.server import build_mcp_server

__all__ = [
    "DEFAULT_CALL_TIMEOUT_S",
    "MCP_HTTP_PATH",
    "CtxProvider",
    "InMemoryMcpTransport",
    "McpAgentClient",
    "McpRuntime",
    "McpSettings",
    "build_mcp_server",
    "mount_registry_on_mcp",
]
