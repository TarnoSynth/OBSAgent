"""Adapter ``ToolRegistry`` → FastMCP (Faza 1 refaktoru agentic tool loop).

Celem tej warstwy jest **1:1 propagacja** wszystkich narzędzi zarejestrowanych
w ``ToolRegistry`` do serwera FastMCP, bez duplikowania logiki ``Tool.execute``.

**Kontrakt:**

- ``mount_registry_on_mcp(mcp, registry, ctx_provider)`` przegląda narzędzia
  z registry (posortowane alfabetycznie przez ``ToolRegistry.names()`` -
  determinizm, kluczowy dla prompt cachingu) i dla każdego dodaje wpis
  do ``mcp._tool_manager._tools``.
- ``input_schema()`` z naszego ``Tool`` jest **dokładnie** tym schematem,
  który FastMCP wystawia przez ``list_tools`` - żadne wrappery, żadna
  inferencja z sygnatury Python.
- ``ctx_provider`` to callable ``() -> ToolExecutionContext`` - pozwala
  agentowi budować świeży context per sesja (per commit projektowy) bez
  restartowania serwera MCP. Serwer żyje cały bieg, context jest ulotny.

**Dlaczego obchodzimy publiczne ``mcp.add_tool``:**

``FastMCP.add_tool(fn)`` buduje schemat z sygnatury Python (``func_metadata``),
co dla naszego generic ``Tool.execute(args: dict, ctx)`` dałoby schemat
``{args: object}`` zamiast konkretnego JSON Schema pól. Zamiast tego
budujemy ``mcp.server.fastmcp.tools.Tool`` ręcznie, z ``parameters`` =
``tool.input_schema()`` i przełącznikiem ``arg_model = PassthroughArgs``
(akceptuje dowolne pola - walidacja semantyczna i tak leci w naszym
``Tool.execute``, bo model Pydantic ``ArgsModel`` żyje po stronie narzędzia).

**Kontrakt zwrotny:**

- ``ToolResult.ok=True``  → MCP zwraca ``content`` jako tekst w
  ``CallToolResult.content`` (lista z jednym ``TextContent``). Żadne
  strukturowane pole nie jest inline'owane (Faza 1: strukturowane idzie
  przez ``ToolResult.to_model_text`` jako część tekstu).
- ``ToolResult.ok=False`` → MCP zwraca ``isError=True`` + tekst błędu.
  Klient ``McpAgentClient`` tłumaczy to z powrotem na ``ToolResult(ok=False)``
  - żadne wyjątki nie lecą przez sieć w pętli tool-use.
- ``tool.execute`` crashuje (bug) → lecimy wyjątkiem (``ToolError``) -
  FastMCP zamieni na MCP error response. Rejestr tego nie obsługuje tu
  bo docelowo dispatch chodzi przez ``ToolRegistry.dispatch`` w Fazie 2+.
  **Ale:** w Fazie 1 wywołujemy ``ToolRegistry.dispatch`` bezpośrednio,
  dzięki czemu crashe już są łapane i mapowane na ``ToolResult(ok=False)``.

**Idempotencja:**

``mount_registry_on_mcp`` można wołać wielokrotnie - ponowny montaż
nadpisuje poprzednie wpisy (ważne dla testów i potencjalnego
hot-reload'u). Produkcyjnie wywoływane raz, w ``build_mcp_server``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools import Tool as FastMCPTool
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase, FuncMetadata
from pydantic import ConfigDict

from src.agent.tools.base import Tool as AgentTool
from src.agent.tools.registry import ToolRegistry
from src.providers.base import ToolCall, ToolFunctionCall

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from src.agent.tools.context import ToolExecutionContext

logger = logging.getLogger(__name__)


CtxProvider = Callable[[], "ToolExecutionContext"]
"""Callable zwracający **aktualny** ``ToolExecutionContext`` dla wywołania narzędzia.

Per sesja agenta (per commit) context się zmienia (świeże ``VaultKnowledge``,
nowe ``executed_actions``). Serwer MCP się nie restartuje, więc dispatch
bierze context "teraz" przez ten getter - agent jest jedynym miejscem,
gdzie ustawia się "bieżący" context.
"""


def _make_passthrough_arg_model(tool_name: str) -> type[ArgModelBase]:
    """Buduje dynamiczny model Pydantic akceptujący dowolne pola.

    FastMCP wywołuje w tej kolejności::

        parsed = arg_model.model_validate(pre_parsed_args)
        kwargs = parsed.model_dump_one_level()
        fn(**kwargs)

    Nie chcemy tutaj walidować nic - konkretny ``Tool`` ma własny ``ArgsModel``
    i walidacja leci w ``tool.execute``. Dlatego:

    - ``extra="allow"`` → pydantic przyjmie dowolne klucze i zachowa je w
      ``__pydantic_extra__``.
    - ``model_dump_one_level`` domyślnie iteruje po ``model_fields`` - czyli
      dla modelu bez pól zwraca **pustego dicta**. Nadpisujemy metodę żeby
      zwracać ``model_extra`` (rzeczywiste przekazane klucze).

    Bez tego override'u wszystkie argumenty modelu gubiły by się między
    walidacją a ``fn(**kwargs)`` - narzędzie dostawałoby puste ``args``.
    """

    class _PassthroughArgs(ArgModelBase):
        model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

        def model_dump_one_level(self) -> dict[str, Any]:
            return dict(self.__pydantic_extra__ or {})

    _PassthroughArgs.__name__ = f"{tool_name}_PassthroughArgs"
    _PassthroughArgs.__qualname__ = _PassthroughArgs.__name__
    return _PassthroughArgs


def _make_dispatch_fn(
    tool: AgentTool,
    registry: ToolRegistry,
    ctx_provider: CtxProvider,
) -> Callable[..., Awaitable[str]]:
    """Buduje closure ``fn(**kwargs)`` dispatchującą wywołanie przez registry.

    Używamy ``registry.dispatch`` (nie ``tool.execute`` bezpośrednio), żeby
    dostać wspólną logikę łapania wyjątków → ``ToolResult(ok=False)``.
    ``ToolCall`` jest zbudowany lokalnie - nazwa + argumenty jako JSON.
    """

    tool_name = tool.name

    async def _dispatch(**kwargs: Any) -> str:
        import json as _json

        ctx = ctx_provider()
        tool_call = ToolCall(
            id=None,
            type="function",
            function=ToolFunctionCall(
                name=tool_name,
                arguments=_json.dumps(kwargs, ensure_ascii=False) if kwargs else "{}",
            ),
        )
        result = await registry.dispatch(tool_call, ctx)

        if not result.ok:
            err = result.error or "unknown error"
            suffix = f"\n\n{result.content}" if result.content else ""
            raise ToolError(f"{err}{suffix}")
        return result.content

    _dispatch.__name__ = f"dispatch_{tool_name}"
    return _dispatch


def mount_registry_on_mcp(
    mcp: "FastMCP",
    registry: ToolRegistry,
    ctx_provider: CtxProvider,
) -> list[str]:
    """Montuje wszystkie narzędzia z ``registry`` na ``mcp``.

    Zwraca listę nazw zamontowanych narzędzi (posortowaną alfabetycznie).
    Jeśli registry jest pusty - zwraca ``[]`` i nie robi nic poza zalogowaniem.

    **Efekt uboczny:** nadpisuje wpisy w ``mcp._tool_manager._tools``
    bezpośrednio. Nie używa publicznego ``mcp.add_tool``, bo ten buduje
    schemat z sygnatury Python (nie umiałby wyrazić naszych JSON Schemas
    z ``Tool.input_schema()``).

    **Dlaczego dostęp do _tool_manager jest OK:** API publiczne FastMCP
    (``add_tool`` + dekorator ``@mcp.tool``) jest opinionated na funkcje
    Python z typowanymi argumentami. Nasza warstwa ``Tool`` jest
    generyczna z arbitralnym JSON Schema i nie ma 1:1 mapowania na
    sygnaturę funkcji. Ręczne wstawienie ``FastMCPTool`` jest jedyną
    drogą, którą udokumentowany MCP SDK rysuje (patrz ``Tool.from_function``
    - które jest helperem, nie jedyną drogą). Gdyby to się zmieniło w
    nowszej wersji ``mcp``, ta funkcja jest jednym punktem do poprawki.
    """

    names = registry.names()
    if not names:
        logger.info("mount_registry_on_mcp: registry pusty - 0 narzędzi zamontowanych")
        return []

    tool_manager = mcp._tool_manager  # type: ignore[attr-defined]

    mounted: list[str] = []
    for name in names:
        tool = registry.get(name)
        if tool is None:
            continue

        arg_model = _make_passthrough_arg_model(name)
        fn = _make_dispatch_fn(tool, registry, ctx_provider)
        fn_metadata = FuncMetadata(arg_model=arg_model)

        mcp_tool = FastMCPTool(
            fn=fn,
            name=tool.name,
            title=None,
            description=tool.description,
            parameters=tool.input_schema(),
            fn_metadata=fn_metadata,
            is_async=True,
            context_kwarg=None,
        )
        tool_manager._tools[name] = mcp_tool  # type: ignore[attr-defined]
        mounted.append(name)

    logger.info("mount_registry_on_mcp: zamontowano %d narzędzi: %s", len(mounted), mounted)
    return mounted


__all__ = ["CtxProvider", "mount_registry_on_mcp"]
