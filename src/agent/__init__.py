"""Publiczne API warstwy agenta \u2014 kompozycja Git + Vault + AI.

Warstwa agenta jest **jedynym miejscem** w projekcie, ktore legalnie importuje
zarowno ``src.git``, jak i ``src.vault``. Tutaj zapadaja decyzje: ktore repo
przeczytac, co zrobic ze zmianami, jak zbudowac prompt dla AI, czy i kiedy
zapisac nowy stan.

Dostepne (Fazy 5 + 6):

**Stan miedzy biegami (Faza 5)**

- ``AgentState``        \u2014 Pydantic: pamiec agenta miedzy biegami
- ``VaultSnapshot``     \u2014 lekkie zdjecie vaulta na koniec biegu
- ``AgentStateStore``   \u2014 persystencja ``AgentState`` w ``.agent-state.json``

**Modele pisow/planow AI (Faza 7 — tool-loop based)**

- ``ProposedWrite``            \u2014 jedna operacja na vaulcie (create/update/append)
- ``ProposedPlan``             \u2014 pelny plan AI (summary + lista pisow)
- ``SessionResult``            \u2014 wynik sesji tool-loop (plan + metryki)
- ``ActionType``               \u2014 ``Literal["create", "update", "append"]``

**Chunking diffow (Faza 6 \u2014 po refaktorze)**

- ``ChunkedCommit``             \u2014 commit podzielony na chunki gotowe do AI
- ``DiffChunk``                 \u2014 pojedynczy chunk diffa (header + hunki)
- ``ChunkSummary``              \u2014 podsumowanie chunka z AI (cache'owane)
- ``ChunkCache``                \u2014 persystentny cache chunkow + podsumowan

**Prompty i szablony (Faza 6)**

- ``load_system_prompt``       \u2014 wczytuje system prompt PL/EN z ``Prompts/``
- ``load_chunk_instruction_prompt`` \u2014 prompt dla chunk-summary (multi-turn)
- ``load_finalize_prompt``     \u2014 prompt FINALIZE po zgromadzeniu summaries
- ``load_template``            \u2014 wczytuje jeden szablon notatki z ``templates/``
- ``load_all_templates``       \u2014 wczytuje wszystkie szablony jako dict
- ``render_template``          \u2014 renderuje szablon z ``{{placeholder}}`` (test/debug)
- ``build_user_prompt``        \u2014 sklada user prompt dla trybu SMALL
- ``build_chunk_summary_prompt`` \u2014 user prompt dla pojedynczej tury chunk-summary
- ``build_finalize_prompt``    \u2014 user prompt dla FINALIZE (multi-turn)

**Silnik akcji (Faza 6)**

- ``GitContextBuilder``        \u2014 chunkowanie diffow + filtrowanie ignore_patterns
- ``ActionExecutor``           \u2014 aplikuje akcje + plany, best-effort
- ``ActionExecutionReport``    \u2014 raport z batcha wykonania
- ``ActionOutcome``            \u2014 wynik pojedynczej akcji (sukces/blad)
- ``PlannedVaultWrite``        \u2014 zaplanowany zapis (MOC / indeks)
- ``plan_post_action_updates`` \u2014 dry-run MOC/indeksu dla listy akcji

**Preview i interakcja (Faza 6)**

- ``PreviewRenderer``          \u2014 rich-formatted tabela planu dla usera
- ``ask_confirm``               \u2014 pytanie "zatwierdz i zacommituj?"
- ``ask_retry``                 \u2014 pytanie "sprobuj ponownie po odrzuceniu?"

**Orkiestracja (Faza 6)**

- ``Agent``                    \u2014 klasa kompozycyjna, atomowe metody biegu
- ``AgentConfig``              \u2014 rozwiazana konfiguracja agenta
- ``ChunkProgressCallback``    \u2014 sygnatura callbacka progresu chunk-summary

Pelna **petla iteracyjna** po commitach zyje w ``main.py`` \u2014 ``Agent``
nie narzuca flow, tylko udostepnia metody (sync, next_commit, prepare,
propose, preview, execute, commit_vault). Dzieki temu main.py czyta sie
jak roadmap, a testy moga pomijac interakcje ze stdin.

**Warstwa narzedzi (Faza 0 refaktoru agentic tool loop)**

- ``tools`` (submodule) \u2014 fundamenty petli tool-use. Dostep przez
  ``from src.agent import tools`` lub ``from src.agent.tools import ...``.
  W Fazie 0 sam szkielet: ``Tool``, ``ToolResult``, ``ToolRegistry``,
  ``ToolExecutionContext``. Konkretne narzedzia pojawia sie fazami.

**Warstwa MCP (Faza 1 refaktoru agentic tool loop)**

- ``mcp`` (submodule) \u2014 lokalny serwer Model Context Protocol (streamable-http)
  wystawiajacy narzedzia z ``ToolRegistry`` na localhost. Dostep przez
  ``from src.agent import mcp`` lub ``from src.agent.mcp import ...``.
  Eksporty: ``McpSettings``, ``McpRuntime``, ``McpAgentClient``,
  ``build_mcp_server``, ``mount_registry_on_mcp``.
"""

from src.agent import mcp, tools
from src.agent.action_executor import (
    ActionExecutionReport,
    ActionExecutor,
    ActionOutcome,
)
from src.agent.agent import Agent, AgentConfig, ChunkProgressCallback
from src.agent.chunk_cache import ChunkCache
from src.agent.git_context import GitContextBuilder
from src.agent.models import AgentState, VaultSnapshot
from src.agent.models_actions import (
    ActionType,
    ProposedPlan,
    ProposedWrite,
    SessionResult,
)
from src.agent.models_chunks import (
    ChunkedCommit,
    ChunkSummary,
    DiffChunk,
)
from src.agent.moc_planner import PlannedVaultWrite, plan_post_action_updates
from src.agent.pending import (
    PENDING_END_MARKER,
    PENDING_START_MARKER,
    PREVIOUS_END_MARKER,
    PREVIOUS_START_MARKER,
    PendingBatch,
    capture_snapshot,
    has_pending_markers,
    has_previous_markers,
    render_display_content,
    restore_from_snapshot,
    wrap_pending,
    wrap_pending_body,
    wrap_previous_body,
)
from src.agent.preview import PreviewRenderer, ask_accept_pending, ask_confirm, ask_retry
from src.agent.prompt_builder import (
    build_chunk_summary_prompt,
    build_finalize_prompt,
    build_user_prompt,
)
from src.agent.prompts import (
    load_chunk_instruction_prompt,
    load_finalize_prompt,
    load_system_prompt,
)
from src.agent.state import (
    DEFAULT_PROCESSED_WINDOW,
    DEFAULT_STATE_FILENAME,
    AgentStateStore,
)
from src.agent.templates import load_all_templates, load_template, render_template

__all__ = [
    "DEFAULT_PROCESSED_WINDOW",
    "DEFAULT_STATE_FILENAME",
    "PENDING_END_MARKER",
    "PENDING_START_MARKER",
    "PREVIOUS_END_MARKER",
    "PREVIOUS_START_MARKER",
    "ActionExecutionReport",
    "ActionExecutor",
    "ActionOutcome",
    "ActionType",
    "Agent",
    "AgentConfig",
    "AgentState",
    "AgentStateStore",
    "ChunkCache",
    "ChunkProgressCallback",
    "ChunkSummary",
    "ChunkedCommit",
    "DiffChunk",
    "GitContextBuilder",
    "PendingBatch",
    "PlannedVaultWrite",
    "PreviewRenderer",
    "ProposedPlan",
    "ProposedWrite",
    "SessionResult",
    "VaultSnapshot",
    "ask_accept_pending",
    "ask_confirm",
    "ask_retry",
    "build_chunk_summary_prompt",
    "build_finalize_prompt",
    "build_user_prompt",
    "capture_snapshot",
    "has_pending_markers",
    "has_previous_markers",
    "load_all_templates",
    "load_chunk_instruction_prompt",
    "load_finalize_prompt",
    "load_system_prompt",
    "load_template",
    "mcp",
    "plan_post_action_updates",
    "render_display_content",
    "render_template",
    "restore_from_snapshot",
    "tools",
    "wrap_pending",
    "wrap_pending_body",
    "wrap_previous_body",
]
