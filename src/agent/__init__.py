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

**Modele akcji AI (Faza 6)**

- ``AgentAction``              \u2014 jedna operacja na vaulcie (create/update/append)
- ``AgentResponse``            \u2014 pelna odpowiedz AI (summary + lista akcji)
- ``ActionType``               \u2014 ``Literal["create", "update", "append"]``
- ``SUBMIT_PLAN_TOOL_NAME``    \u2014 nazwa narzedzia tool callingu
- ``build_submit_plan_schema`` \u2014 generator JSON Schema z modelu Pydantic

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
"""

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
    SUBMIT_PLAN_TOOL_DESCRIPTION,
    SUBMIT_PLAN_TOOL_NAME,
    ActionType,
    AgentAction,
    AgentResponse,
    build_submit_plan_schema,
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
    "SUBMIT_PLAN_TOOL_DESCRIPTION",
    "SUBMIT_PLAN_TOOL_NAME",
    "ActionExecutionReport",
    "ActionExecutor",
    "ActionOutcome",
    "ActionType",
    "Agent",
    "AgentAction",
    "AgentConfig",
    "AgentResponse",
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
    "VaultSnapshot",
    "ask_accept_pending",
    "ask_confirm",
    "ask_retry",
    "build_chunk_summary_prompt",
    "build_finalize_prompt",
    "build_submit_plan_schema",
    "build_user_prompt",
    "capture_snapshot",
    "has_pending_markers",
    "has_previous_markers",
    "load_all_templates",
    "load_chunk_instruction_prompt",
    "load_finalize_prompt",
    "load_system_prompt",
    "load_template",
    "plan_post_action_updates",
    "render_display_content",
    "render_template",
    "restore_from_snapshot",
    "wrap_pending",
    "wrap_pending_body",
    "wrap_previous_body",
]
