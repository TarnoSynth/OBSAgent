"""``ToolRegistry`` \u2014 rejestr narzedzi + dispatch ``ToolCall`` \u2192 ``ToolResult``.

**Rola w petli tool-use (docelowo, Faza 1):**

::

    messages = [system, user_with_commit]
    ctx = ToolExecutionContext(vault_manager=..., commit_info=..., ...)
    for step in range(max_tool_iterations):
        result = await provider.complete(
            ChatRequest(messages=messages, tools=registry.tool_definitions())
        )
        messages.append(assistant_msg(result))
        if not result.tool_calls:
            break
        for tool_call in result.tool_calls:
            tool_result = await registry.dispatch(tool_call, ctx)
            messages.append(tool_result_msg(tool_call.id, tool_result.to_model_text()))
        if ctx.finalized:
            break

Rejestr jest **cienki** \u2014 nie robi nic ponad: trzyma slownik narzedzi po
``name``, buduje liste ``ToolDefinition`` dla providera, parsuje argumenty
JSON, wywoluje ``tool.execute``. Caly stan sesji zyje w ``ToolExecutionContext``.

**Kontrakty dispatcha:**

- Gdy nazwa narzedzia nieznana \u2192 ``ToolResult(ok=False, error=...)``.
  Model widzi "Unknown tool" i moze sie poprawic.
- Gdy argumenty nie parsuja sie jako JSON object \u2192 ``ok=False, error=...``.
  (OpenAI/Anthropic zwracaja ``arguments`` jako string z JSON-em.)
- Gdy ``tool.execute`` rzuci wyjatkiem \u2192 ``ok=False, error="Tool crashed: ..."``
  + logger.exception. **To jest sygnal buga**, nie domenowy blad.
  Domenowe bledy narzedzie sygnalizuje przez wlasny ``ToolResult(ok=False)``.

**Idempotencja rejestru:**

``register`` rzuca ``ValueError`` przy duplikacie nazwy (signal buga).
``unregister`` jest silent \u2014 brak narzedzia = no-op. ``get`` zwraca ``None``
dla nieznanej nazwy (caller decyduje co z tym zrobic).

**Determinizm wyjscia:**

``tool_definitions()`` i ``names()`` zwracaja zsortowane alfabetycznie \u2014
kolejnosc narzedzi w prompcie modelu jest stabilna miedzy biegami. Wplyw:
prompt caching (Anthropic) moze cachowac te same tokeny.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from src.agent.tools.base import Tool, ToolResult
from src.providers.base import ToolCall, ToolDefinition, ToolFunctionDefinition

if TYPE_CHECKING:
    from src.agent.tools.context import ToolExecutionContext

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Rejestr narzedzi agenta + dispatcher wywolan.

    Pusty rejestr jest legalnym stanem \u2014 ``tool_definitions()`` zwraca ``[]``,
    a model dostaje pusta liste ``tools`` w prompcie (tool calling wylaczony
    dla tej sesji). To UZYTECZNE dla sciezki "agent bez narzedzi" (legacy)
    i dla testow, ktore nie chca mockowac zadnego narzedzia.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Dodaje narzedzie do rejestru. ``ValueError`` przy duplikacie.

        Duplikat = bug (dwa narzedzia o tej samej nazwie to niespojny
        kontrakt dla modelu). Silent override moglby zamaskowac regresje.
        """

        if not isinstance(tool, Tool):
            raise TypeError(
                f"ToolRegistry.register oczekuje instancji Tool, dostalo {type(tool).__name__}"
            )
        name = tool.name
        if not name or not isinstance(name, str):
            raise ValueError("Tool.name musi byc niepustym stringiem")
        if name in self._tools:
            raise ValueError(f"Tool '{name}' jest juz zarejestrowany")
        self._tools[name] = tool

    def unregister(self, name: str) -> None:
        """Usuwa narzedzie z rejestru \u2014 no-op jesli nie bylo zarejestrowane."""

        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Zwraca narzedzie po nazwie albo ``None``."""

        return self._tools.get(name)

    def names(self) -> list[str]:
        """Zwraca posortowana alfabetycznie liste nazw zarejestrowanych narzedzi."""

        return sorted(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def tool_definitions(self) -> list[ToolDefinition]:
        """Buduje liste ``ToolDefinition`` do przekazania providerowi LLM.

        Kolejnosc: posortowana po nazwie narzedzia (determinizm promptu).
        Puste narzedzia \u2192 pusta lista \u2014 caller (Agent) zdecyduje, czy
        wywolywac providera z ``tools=[]`` czy pominac tool calling.
        """

        definitions: list[ToolDefinition] = []
        for name in sorted(self._tools.keys()):
            tool = self._tools[name]
            definitions.append(
                ToolDefinition(
                    type="function",
                    function=ToolFunctionDefinition(
                        name=tool.name,
                        description=tool.description,
                        parameters=tool.input_schema(),
                    ),
                )
            )
        return definitions

    async def dispatch(
        self,
        tool_call: ToolCall,
        ctx: "ToolExecutionContext",
    ) -> ToolResult:
        """Wykonuje pojedyncze wywolanie narzedzia z modelu.

        **Sciezki:**

        1. Nieznana nazwa narzedzia \u2192 ``ok=False``, model dostaje blad.
        2. Argumenty to nie poprawny JSON \u2192 ``ok=False``.
        3. Argumenty to JSON ale nie object (np. lista) \u2192 ``ok=False``.
        4. ``tool.execute`` rzuca wyjatkiem \u2192 ``ok=False, error="Tool crashed..."``,
           logger.exception \u2014 to jest BUG, nie domenowy blad.
        5. Wszystko ok \u2192 wynik z ``tool.execute`` bez modyfikacji.

        Nie loguje ``tool.dispatch.*`` eventow \u2014 to zadanie Agenta (Faza 1),
        ktory ma dostep do ``RunLogger`` i wie w jakiej jest turze petli.
        """

        fn_name = tool_call.function.name
        tool = self._tools.get(fn_name)
        if tool is None:
            return ToolResult(
                ok=False,
                error=f"Unknown tool: {fn_name!r}. Available: {', '.join(self.names()) or '(none)'}",
            )

        raw_args = tool_call.function.arguments or ""
        if raw_args.strip() == "":
            args: dict = {}
        else:
            try:
                parsed = json.loads(raw_args)
            except json.JSONDecodeError as exc:
                return ToolResult(
                    ok=False,
                    error=f"Invalid JSON in tool arguments: {exc.msg} at pos {exc.pos}",
                )
            if not isinstance(parsed, dict):
                return ToolResult(
                    ok=False,
                    error=f"Tool arguments must be a JSON object, got {type(parsed).__name__}",
                )
            args = parsed

        try:
            result = await tool.execute(args, ctx)
        except Exception as exc:  # noqa: BLE001 \u2014 celowo szeroki catch dla "bug in tool"
            logger.exception("Tool %s crashed during execute", fn_name)
            return ToolResult(
                ok=False,
                error=f"Tool '{fn_name}' crashed: {type(exc).__name__}: {exc}",
            )

        if not isinstance(result, ToolResult):
            logger.error(
                "Tool %s zwrocil nie-ToolResult (%s) \u2014 potraktowane jako blad",
                fn_name,
                type(result).__name__,
            )
            return ToolResult(
                ok=False,
                error=f"Tool '{fn_name}' zwrocil niepoprawny typ: {type(result).__name__}",
            )

        return result


__all__ = ["ToolRegistry"]
