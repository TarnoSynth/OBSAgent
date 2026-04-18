"""``McpSettings`` - konfiguracja lokalnego serwera MCP (Faza 1 refaktoru).

Sekcja ``mcp:`` w ``config.yaml`` jest opcjonalna - gdy nie ma lub ``enabled: false``,
agent idzie starą ścieżką (bezpośredni ``ToolRegistry`` bez HTTP). W produkcji
zawsze ``enabled: true``.

**Domyślne wartości (zgodne z REFACTOR_PLAN.md):**

- ``host = "127.0.0.1"`` - tylko loopback; zmiana wymaga świadomego editu
  configu, bo serwer nie ma autoryzacji.
- ``port = 8765`` - arbitralny, nieużywany przez popularne usługi. Przy
  kolizji user zmienia w configu.
- ``transport = "streamable-http"`` - jedyny wspierany transport w Fazie 1.
  Klient łączy się do ``http://{host}:{port}/mcp`` (endpoint FastMCP domyślny).
- ``startup_timeout_s = 5.0`` - ile sekund czekać na ready health check.
- ``enabled = True`` - MCP runtime wstaje wraz z agentem.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_SERVER_NAME = "obsagent"
DEFAULT_TRANSPORT: Literal["streamable-http"] = "streamable-http"
DEFAULT_STARTUP_TIMEOUT_S = 5.0
DEFAULT_ENABLED = True

#: Ścieżka HTTP pod którą FastMCP wystawia streamable-http endpoint.
#: Odpowiada ``streamable_http_path`` w ``FastMCP.__init__``.
MCP_HTTP_PATH = "/mcp"


@dataclass(slots=True)
class McpSettings:
    """Rozwiązana konfiguracja warstwy MCP.

    Budowana przez ``McpSettings.from_config(path)`` z ``config.yaml``.
    Wszystkie pola mają sensowne defaulty - user nadpisuje punktowo.
    """

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    server_name: str = DEFAULT_SERVER_NAME
    transport: Literal["streamable-http"] = DEFAULT_TRANSPORT
    startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S
    enabled: bool = DEFAULT_ENABLED

    @property
    def url(self) -> str:
        """Pełny URL streamable-http endpointu (``http://host:port/mcp``).

        Klient MCP (``McpAgentClient``) łączy się dokładnie pod tym URL-em.
        """

        return f"http://{self.host}:{self.port}{MCP_HTTP_PATH}"

    @classmethod
    def from_config(cls, config_path: str | Path) -> "McpSettings":
        """Czyta sekcję ``mcp:`` z ``config.yaml`` i buduje obiekt.

        Brak sekcji albo brak pliku -> wszystkie defaulty.
        Niepoprawne wartości (zły typ, port poza zakresem, itd.) -> ``ValueError``.
        """

        resolved = Path(config_path).expanduser().resolve()
        if not resolved.is_file():
            return cls()

        try:
            raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"config: nie udało się wczytać YAML z {resolved}: {exc}") from exc

        if not isinstance(raw, dict):
            return cls()
        section = raw.get("mcp")
        if section is None:
            return cls()
        if not isinstance(section, dict):
            raise ValueError("config: sekcja 'mcp' musi być mapą (dict)")

        return cls._from_dict(section)

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "McpSettings":
        """Wewnętrzny parser - dict -> ``McpSettings`` z walidacją."""

        host = data.get("host", DEFAULT_HOST)
        if not isinstance(host, str) or not host.strip():
            raise ValueError("config: mcp.host musi być niepustym stringiem")

        port_raw = data.get("port", DEFAULT_PORT)
        try:
            port = int(port_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("config: mcp.port musi być liczbą całkowitą") from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"config: mcp.port={port} poza zakresem 1..65535")

        server_name = data.get("server_name", DEFAULT_SERVER_NAME)
        if not isinstance(server_name, str) or not server_name.strip():
            raise ValueError("config: mcp.server_name musi być niepustym stringiem")

        transport_raw = data.get("transport", DEFAULT_TRANSPORT)
        if transport_raw != "streamable-http":
            raise ValueError(
                f"config: mcp.transport={transport_raw!r} - w Fazie 1 wspierany jest "
                f"tylko 'streamable-http'. Patrz REFACTOR_PLAN.md (Faza 7) dla planu stdio/sse."
            )

        startup_timeout_raw = data.get("startup_timeout_s", DEFAULT_STARTUP_TIMEOUT_S)
        try:
            startup_timeout = float(startup_timeout_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("config: mcp.startup_timeout_s musi być liczbą") from exc
        if startup_timeout <= 0:
            raise ValueError("config: mcp.startup_timeout_s musi być > 0")

        enabled_raw = data.get("enabled", DEFAULT_ENABLED)
        if not isinstance(enabled_raw, bool):
            raise ValueError("config: mcp.enabled musi być true/false")

        return cls(
            host=host.strip(),
            port=port,
            server_name=server_name.strip(),
            transport="streamable-http",
            startup_timeout_s=startup_timeout,
            enabled=enabled_raw,
        )


__all__ = ["McpSettings", "MCP_HTTP_PATH"]
