"""Publiczne API warstwy narzedzi agenta (Faza 0 refaktoru agentic tool loop).

**Stan na Faze 0:** sam szkielet \u2014 klasy bazowe + rejestr + context.
Konkretnych narzedzi jeszcze nie ma (pojawia sie fazami w ``src/agent/tools/vault_read/``,
``src/agent/tools/vault_write/``, ``src/agent/tools/session/``).

**Eksporty:**

- ``Tool``                 \u2014 ABC dla pojedynczego narzedzia (``name``, ``description``,
                              ``input_schema()``, ``execute()``)
- ``ToolResult``           \u2014 znormalizowany wynik ``tool.execute`` (Pydantic)
- ``ToolExecutionContext`` \u2014 stan dzielony miedzy wywolaniami narzedzi
                              w jednej sesji agenta (per commit)
- ``ToolRegistry``         \u2014 rejestr + dispatcher ``ToolCall`` \u2192 ``ToolResult``

**Rola warstwy w docelowej architekturze:**

Gdy model LLM wchodzi w petle tool-use dla jednego commita projektowego,
``Agent`` buduje ``ToolRegistry`` wypelniony wszystkimi narzedziami,
tworzy jeden ``ToolExecutionContext`` na sesje i iteruje:

::

    while not ctx.finalized and step < max_tool_iterations:
        result = await provider.complete(
            ChatRequest(messages=..., tools=registry.tool_definitions())
        )
        for tc in result.tool_calls:
            tool_result = await registry.dispatch(tc, ctx)
            messages.append(tool_result_msg(tc.id, tool_result.to_model_text()))

Warstwa jest **agnostyczna wzgledem providera** \u2014 ``Tool.input_schema()``
zwraca generic JSON Schema, ``ToolDefinition`` z ``src.providers`` jest
formatem wspolnym dla OpenAI/Anthropic/OpenRouter (patrz ADR__ToolCalling).

**Konwencje nazewnicze narzedzi:**

- ``list_*``, ``read_*``, ``find_*``, ``get_*`` \u2014 eksploracja (read-only)
- ``create_*``                                  \u2014 tworzenie nowej notatki
- ``append_*``, ``replace_*``, ``add_*``, ``update_*`` \u2014 modyfikacje in-place
- ``register_*``                                 \u2014 rejestracja metadanych
                                                  (np. pending concepts)
- ``submit_plan``                                \u2014 terminator sesji
"""

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext
from src.agent.tools.registry import ToolRegistry
from src.agent.tools.session import SubmitPlanTool
from src.agent.tools.vault_read import (
    FindRelatedTool,
    GetCommitContextTool,
    ListNotesTool,
    ListPendingConceptsTool,
    ListTagsTool,
    ReadNoteTool,
    VaultMapTool,
)
from src.agent.tools.vault_write import (
    AddMocLinkTool,
    AddRelatedLinkTool,
    AddTableRowTool,
    AppendSectionTool,
    AppendToNoteTool,
    CreateChangelogEntryTool,
    CreateConceptTool,
    CreateDecisionTool,
    CreateHubTool,
    CreateModuleTool,
    CreateNoteTool,
    CreateTechnologyTool,
    RegisterPendingConceptTool,
    ReplaceSectionTool,
    UpdateFrontmatterTool,
    UpdateNoteTool,
)

__all__ = [
    "AddMocLinkTool",
    "AddRelatedLinkTool",
    "AddTableRowTool",
    "AppendSectionTool",
    "AppendToNoteTool",
    "CreateChangelogEntryTool",
    "CreateConceptTool",
    "CreateDecisionTool",
    "CreateHubTool",
    "CreateModuleTool",
    "CreateNoteTool",
    "CreateTechnologyTool",
    "FindRelatedTool",
    "GetCommitContextTool",
    "ListNotesTool",
    "ListPendingConceptsTool",
    "ListTagsTool",
    "ReadNoteTool",
    "RegisterPendingConceptTool",
    "ReplaceSectionTool",
    "SubmitPlanTool",
    "Tool",
    "ToolExecutionContext",
    "ToolRegistry",
    "ToolResult",
    "UpdateFrontmatterTool",
    "UpdateNoteTool",
    "VaultMapTool",
]
