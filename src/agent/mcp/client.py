"""``McpAgentClient`` - klient MCP osadzony w agencie (Faza 1 refaktoru).

**Rola:** tłumaczy prymitywy pętli tool-use agenta na wywołania protokołu MCP.

- ``list_tools()`` - pobiera listę narzędzi raz na sesję (cache), mapuje
  ``mcp.types.Tool`` → ``src.providers.base.ToolDefinition`` (format zrozumiały
  dla providerów OpenAI/Anthropic/OpenRouter). Wynik jest **niezmienną
  listą** - ta sama referencja leci do każdego ``ChatRequest.tools`` →
  Anthropic prompt caching widzi stabilny prefix.
- ``call_tool(name, args)`` - pojedyncze wywołanie narzędzia. Mapuje
  ``mcp.types.CallToolResult`` → ``ToolResult`` (``ok`` zależy od
  ``isError``, ``content`` z tekstowych części). Błędy protokolu nie
  wyciekają jako wyjątki - lądują w ``ToolResult(ok=False, error=...)``,
  czyli model dostaje "ERROR: ..." i ma szansę się poprawić.
- ``connect()``/``close()`` - lifecycle sesji MCP (streamable-http transport).
  Idempotentne. Zarządzane przez agenta w pętli ``run_session`` (Faza 2).

**Thread safety:** klient jest synchroniczny per event loop. Jeden klient
per Agent (per proces). Wywołania ``call_tool`` są sekwencjonowane przez
``asyncio.Lock`` żeby uniknąć race conditions w sesji MCP - transport
``streamable-http`` teoretycznie wspiera concurrent requests, ale nasza
pętla i tak pracuje sekwencyjnie (ta sama instancja ``ToolExecutionContext``
jest mutowalna, a pędzenie tool callów równolegle byłoby niebezpieczne).

**Co NIE robi klient w Fazie 1:**

- Nie cachuje wyniku ``call_tool`` - każde wywołanie idzie do serwera.
- Nie robi retry na sieciowych błędach (timeout lub error → ``ToolResult(ok=False)``).
  Retry na sensownych błędach (429, 503) to Faza 2+.
- Nie obsługuje MCP resources/prompts - tylko tools.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from src.agent.tools.base import ToolResult
from src.providers.base import ToolDefinition, ToolFunctionDefinition

if TYPE_CHECKING:
    from logs.run_logger import RunLogger

    from src.agent.mcp.config import McpSettings

logger = logging.getLogger(__name__)


DEFAULT_CALL_TIMEOUT_S = 30.0


class McpAgentClient:
    """Klient MCP dla agenta - cienki wrapper nad ``ClientSession``.

    Lifecycle::

        client = McpAgentClient(settings=..., run_logger=...)
        await client.connect()
        tools = await client.list_tools()   # cachowane
        result = await client.call_tool("list_notes", {"type": "module"})
        await client.close()

    Context manager też dostępny: ``async with McpAgentClient(...) as client: ...``.
    """

    def __init__(
        self,
        *,
        settings: "McpSettings",
        run_logger: "RunLogger | None" = None,
        call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
    ) -> None:
        self._settings = settings
        self._run_logger = run_logger
        self._call_timeout_s = call_timeout_s

        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._call_lock = asyncio.Lock()

        self._cached_tools: list[ToolDefinition] | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        """``True`` gdy klient ma aktywną sesję MCP z serwerem."""

        return self._connected

    @property
    def url(self) -> str:
        """URL do którego klient się łączy (``McpSettings.url``)."""

        return self._settings.url

    async def connect(self) -> None:
        """Łączy się z serwerem MCP pod ``settings.url``. Idempotentne."""

        if self._connected:
            return

        stack = AsyncExitStack()
        try:
            read_stream, write_stream, _get_session_id = await stack.enter_async_context(
                streamablehttp_client(self._settings.url)
            )
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
        except Exception:
            await stack.aclose()
            raise

        self._exit_stack = stack
        self._session = session
        self._connected = True
        self._cached_tools = None

        if self._run_logger is not None:
            self._run_logger.log_mcp_client_connected(url=self._settings.url)
        logger.info("MCP client połączony: %s", self._settings.url)

    async def ensure_connected(self) -> None:
        """Łączy się jeśli nie podłączony. Wygodny alias dla ``connect()``."""

        if not self._connected:
            await self.connect()

    async def close(self) -> None:
        """Zamyka sesję MCP. Idempotentne."""

        if not self._connected:
            return

        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception as exc:
                logger.warning("MCP client close zwrócił błąd: %r", exc)

        self._exit_stack = None
        self._session = None
        self._connected = False
        self._cached_tools = None

        if self._run_logger is not None:
            self._run_logger.log_mcp_client_closed(url=self._settings.url)
        logger.info("MCP client zamknięty: %s", self._settings.url)

    async def __aenter__(self) -> "McpAgentClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    # ------------------------------------------------------------------

    async def list_tools(self) -> list[ToolDefinition]:
        """Zwraca listę narzędzi serwera MCP. Cache per connection.

        Pierwsze wywołanie idzie do serwera; kolejne zwracają cache. Cache
        jest unieważniany przez ``refresh_tools()`` albo ponowne
        ``connect()``. Ta sama referencja jest zwracana za każdym razem -
        agent może ją przekazywać jako ``ChatRequest.tools`` bez budowania
        nowych obiektów (prompt cache friendly).
        """

        if self._cached_tools is not None:
            if self._run_logger is not None:
                self._run_logger.log_mcp_client_list_tools(
                    count=len(self._cached_tools), from_cache=True
                )
            return self._cached_tools

        session = self._require_session()
        result = await session.list_tools()

        definitions: list[ToolDefinition] = []
        for mcp_tool in result.tools:
            definitions.append(
                ToolDefinition(
                    type="function",
                    function=ToolFunctionDefinition(
                        name=mcp_tool.name,
                        description=mcp_tool.description or "",
                        parameters=dict(mcp_tool.inputSchema or {}),
                    ),
                )
            )

        self._cached_tools = definitions
        if self._run_logger is not None:
            self._run_logger.log_mcp_client_list_tools(
                count=len(definitions), from_cache=False
            )
        return definitions

    async def refresh_tools(self) -> list[ToolDefinition]:
        """Zrzuca cache i pobiera listę narzędzi świeżo z serwera.

        W Fazach 2-5 normalny flow NIE woła tego - registry narzędzi jest
        statyczny w runtime. Metoda istnieje dla testów i ewentualnego
        hot-reload'u w przyszłości.
        """

        self._cached_tools = None
        return await self.list_tools()

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Wywołuje narzędzie ``name`` z argumentami ``args``. Zawsze zwraca ``ToolResult``.

        **Żaden wyjątek nie wycieka na zewnątrz** - wszystkie błędy sieciowe,
        timeout, MCP protocol errors są mapowane na ``ToolResult(ok=False, error=...)``.
        To jest decyzja kontraktowa: model LLM widzi "ERROR: ..." i ma szansę
        się poprawić, zamiast cały flow agenta się wywalał na tranzycyjnym
        problemie.
        """

        session = self._require_session()

        if self._run_logger is not None:
            self._run_logger.log_mcp_call_tool_started(name=name)

        async with self._call_lock:
            try:
                call_result = await asyncio.wait_for(
                    session.call_tool(name, arguments=args or {}),
                    timeout=self._call_timeout_s,
                )
            except asyncio.TimeoutError:
                err = f"mcp timeout after {self._call_timeout_s}s"
                if self._run_logger is not None:
                    self._run_logger.log_mcp_call_tool_failed(name=name, error=err)
                return ToolResult(ok=False, error=err)
            except Exception as exc:
                err = f"mcp call failed: {type(exc).__name__}: {exc}"
                if self._run_logger is not None:
                    self._run_logger.log_mcp_call_tool_failed(name=name, error=err)
                logger.exception("call_tool %s niespodziewany błąd", name)
                return ToolResult(ok=False, error=err)

        content_text = _extract_text_content(call_result)
        is_error = bool(call_result.isError)

        if is_error:
            if self._run_logger is not None:
                self._run_logger.log_mcp_call_tool_failed(name=name, error=content_text or "tool error")
            return ToolResult(
                ok=False,
                error=content_text or f"tool '{name}' returned error",
            )

        if self._run_logger is not None:
            self._run_logger.log_mcp_call_tool_ok(name=name, content_len=len(content_text))
        return ToolResult(ok=True, content=content_text)

    # ------------------------------------------------------------------

    def _require_session(self) -> ClientSession:
        """Zwraca aktywną sesję lub rzuca ``RuntimeError``."""

        if self._session is None or not self._connected:
            raise RuntimeError(
                "McpAgentClient nie jest połączony - zawołaj connect() przed list_tools/call_tool."
            )
        return self._session


def _extract_text_content(result: Any) -> str:
    """Wyciąga tekstową zawartość z ``CallToolResult``.

    MCP 1.x zwraca ``content: list[TextContent | ImageContent | ...]``.
    Dla Fazy 1 interesują nas tylko ``TextContent.text`` - konkatynujemy
    wszystkie kawałki znak-końca-linii. Inne typy (obrazy) są ignorowane
    - przyjdą w Fazie 5+ jeśli kiedykolwiek.
    """

    content = getattr(result, "content", None) or []
    texts: list[str] = []
    for part in content:
        text = getattr(part, "text", None)
        if isinstance(text, str):
            texts.append(text)
    return "\n".join(texts)


__all__ = ["McpAgentClient", "DEFAULT_CALL_TIMEOUT_S"]
