"""Klasa ``Agent`` \u2014 kompozycja Git + Vault + AI dla Fazy 6.

**Zasada:** cala logika agenta (prompt building, wywolania AI, wykonanie
akcji, commit vaulta) zyje TUTAJ jako metody klasy. **Petla iteracyjna**
po commitach projektu zyje w ``main.py`` \u2014 on orkiestruje wolania metod
``Agent`` w odpowiedniej kolejnosci.

Dzieki temu podzialowi:

- ``Agent`` ma pelna wiedze o wszystkich zaleznosciach (DI przez
  ``from_config``), ale nie narzuca flow;
- ``main.py`` czyta sie jak roadmap: `sync → next_commit → propose →
  preview → confirm → execute → commit → mark_processed → save_state`;
- testy jednostkowe moga mockowa\u0107 metody ``Agent`` niezaleznie od
  petli (petla w main.py wymaga interakcji ze stdin i jest trudna
  do testowania \u2014 nie dlatego metody sa tutaj).

Komunikacja z AI idzie **wylacznie** przez tool calling: agent uruchamia
serwer MCP z wszystkimi narzedziami i iteruje w petli tool-use az model
zawola terminator ``submit_plan``. Retry 2x z bledem w prompcie (Q5).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from git import Actor, GitCommandError, Repo
from pydantic import ValidationError

if TYPE_CHECKING:
    from logs.run_logger import RunLogger
    from src.vault.moc import BootstrapMocOutcome

from src.agent.action_executor import ActionExecutionReport, ActionExecutor
from src.agent.chunk_cache import ChunkCache
from src.agent.git_context import GitContextBuilder
from src.agent.mcp import (
    McpAgentClient,
    McpRuntime,
    McpSettings,
    build_mcp_server,
)
from src.agent.pending import PendingBatch
from src.agent.tools import (
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
    FindRelatedTool,
    GetCommitContextTool,
    ListNotesTool,
    ListPendingConceptsTool,
    ListTagsTool,
    MocAuditTool,
    MocSetIntroTool,
    ReadNoteTool,
    RegisterPendingConceptTool,
    ReplaceSectionTool,
    SubmitPlanTool,
    Tool,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
    UpdateFrontmatterTool,
    UpdateNoteTool,
    VaultMapTool,
)
from src.agent.models import AgentState, VaultSnapshot
from src.agent.models_actions import (
    ProposedPlan,
    ProposedWrite,
    SessionResult,
)
from src.agent.models_chunks import ChunkSummary, ChunkedCommit, DiffChunk
from src.agent.moc_planner import PlannedVaultWrite, plan_post_action_updates
from src.agent.prompts import (
    load_chunk_instruction_prompt,
    load_finalize_prompt,
    load_system_prompt,
)
from src.agent.prompt_builder import (
    build_chunk_summary_prompt,
    build_finalize_prompt,
    build_user_prompt,
)
from src.agent.state import AgentStateStore
from src.agent.templates import load_all_examples, load_all_templates
from src.git.models import CommitInfo
from src.git.reader import GitReader
from src.git.syncer import GitSyncer
from src.providers import (
    BaseProvider,
    ChatMessage,
    ChatRequest,
    MessageRole,
    ToolCall,
    build_provider,
    load_config_dict,
)
from src.vault.manager import VaultManager
from src.vault.models import VaultKnowledge, VaultNote

from logs.context import LLMCallContext, llm_call_context

logger = logging.getLogger(__name__)


DEFAULT_MAX_RETRIES = 2
DEFAULT_LANGUAGE = "pl"
DEFAULT_PROJECT_NAME_FALLBACK = "project"
DEFAULT_VAULT_COMMIT_PREFIX = "Agent: sync z "
DEFAULT_VAULT_INDEX_FILENAME = "_index.md"
DEFAULT_BOOTSTRAP_MOC_ENABLED = True
DEFAULT_BOOTSTRAP_MOC_NAME = "Kompendium"
DEFAULT_BOOTSTRAP_MOC_COMMIT_PREFIX = "Agent: bootstrap "
DEFAULT_MAX_TOOL_ITERATIONS = 8
DEFAULT_FORCE_SUBMIT_IN_LAST_N = 2
"""Ile OSTATNICH iteracji petli tool-use ma wymuszac ``submit_plan``.

Gdy ``iterations_left <= DEFAULT_FORCE_SUBMIT_IN_LAST_N``, agent nadpisuje
``tool_choice`` w ``ChatRequest`` na ``{"type": "tool", "name": "submit_plan"}``
— provider Anthropic (i OpenAI) zmusza model do wywolania tego narzedzia.
Chroni przed patologia "model zapomina o terminatorze i zjada caly budzet".
"""

DEFAULT_BUDGET_HINT_LAST_N = 5
"""Ile OSTATNICH iteracji ma dostawac dopisek o pozostalym budzecie w ``tool_result``.

Wlaczamy dopiero przy zblizaniu sie do limitu, zeby nie spamowac wczesniejszych
iteracji. Dopisek idzie jako **suffix** tool_result content, nie nowy system
message - dzieki temu nie rozbija Anthropic prompt cachingu prefiksu.
"""


@dataclass(slots=True)
class AgentConfig:
    """Rozwiazana konfiguracja agenta \u2014 wynik ``Agent.from_config``.

    Trzymana osobno od samej klasy, zeby ``Agent`` byl testowalny przez
    wstrzyk depsow bez obowiazku yamla.
    """

    config_path: Path
    project_repo_path: Path
    vault_path: Path
    language: str
    max_retries: int
    default_commits: int
    project_name: str
    vault_commit_prefix: str
    vault_index_filename: str
    vault_moc_pattern: str
    max_tool_iterations: int
    force_submit_in_last_n: int
    budget_hint_last_n: int
    bootstrap_moc_enabled: bool
    bootstrap_moc_name: str
    bootstrap_moc_title: str | None


class Agent:
    """Kompozycja Git + Vault + AI dla synchronizacji dokumentacji.

    Metody reprezentuja **atomy** petli iteracyjnej (jeden commit
    projektowy = jedno wywolanie calego lancucha metod). Petla siedzi
    w ``main.py``, nie tutaj.

    Kontrakt bezpieczenstwa:

    - **Metoda** ``commit_vault`` jest **jedynym miejscem**, gdzie agent
      wola ``git commit`` na vault.
    - ``commit_vault`` sprawdza argument ``approved=True`` i rzuca
      ``RuntimeError``, gdy false. Druga linia obrony po user flow.
    - Push NIE jest wolany nigdzie. Nigdy. Pozostaje po stronie pluginu
      Obsidian Git.
    """

    def __init__(
        self,
        *,
        config: AgentConfig,
        provider: BaseProvider,
        git_project_reader: GitReader,
        git_vault_reader: GitReader,
        git_project_syncer: GitSyncer,
        git_vault_syncer: GitSyncer,
        vault_manager: VaultManager,
        git_context_builder: GitContextBuilder,
        state_store: AgentStateStore,
        chunk_cache: ChunkCache,
        tool_registry: ToolRegistry | None = None,
        mcp_settings: McpSettings | None = None,
        run_logger: "RunLogger | None" = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.git_project_reader = git_project_reader
        self.git_vault_reader = git_vault_reader
        self.git_project_syncer = git_project_syncer
        self.git_vault_syncer = git_vault_syncer
        self.vault_manager = vault_manager
        self.git_context_builder = git_context_builder
        self.state_store = state_store
        self.chunk_cache = chunk_cache
        self.action_executor = ActionExecutor(vault_manager)
        self.run_logger = run_logger

        self.tool_registry = tool_registry if tool_registry is not None else ToolRegistry()
        self.mcp_settings = mcp_settings if mcp_settings is not None else McpSettings()

        self._current_tool_ctx: ToolExecutionContext | None = None
        self._mcp_runtime: McpRuntime | None = None
        self._mcp_client: McpAgentClient | None = None

        self._system_prompt_cache: str | None = None
        self._chunk_instruction_prompt_cache: str | None = None
        self._finalize_prompt_cache: str | None = None
        self._templates_cache: dict[str, str] | None = None

    @classmethod
    def from_config(
        cls,
        config_path: str | Path,
        *,
        run_logger: "RunLogger | None" = None,
    ) -> "Agent":
        """Factory budujace pelny graf zaleznosci z ``config.yaml`` + ``.env``.

        ``run_logger`` jest opcjonalny — gdy przekazany, provider zostanie
        owiniety w ``LoggingProvider`` i Agent bedzie zglaszal ustrukturyzowane
        eventy (``commit.started``, ``llm.call.*``, itp.).
        """

        resolved = Path(config_path).expanduser().resolve()
        cfg = load_config_dict(resolved)

        paths = cfg.get("paths") or {}
        if not isinstance(paths, dict):
            raise ValueError("config: sekcja 'paths' musi byc mapa")
        project_repo = paths.get("project_repo")
        vault = paths.get("vault")
        if not project_repo or not vault:
            raise ValueError("config: paths.project_repo i paths.vault sa wymagane")

        agent_cfg = cfg.get("agent") or {}
        if not isinstance(agent_cfg, dict):
            raise ValueError("config: sekcja 'agent' musi byc mapa")

        language = str(agent_cfg.get("language") or DEFAULT_LANGUAGE).strip().lower()
        try:
            max_retries = int(agent_cfg.get("max_retries", DEFAULT_MAX_RETRIES))
        except (TypeError, ValueError) as exc:
            raise ValueError("config: agent.max_retries musi byc liczba") from exc
        if max_retries < 0:
            raise ValueError("config: agent.max_retries musi byc >= 0")

        try:
            default_commits = int(agent_cfg.get("default_commits", 10))
        except (TypeError, ValueError) as exc:
            raise ValueError("config: agent.default_commits musi byc liczba") from exc
        if default_commits < 1:
            raise ValueError("config: agent.default_commits musi byc >= 1")

        try:
            max_tool_iterations = int(
                agent_cfg.get("max_tool_iterations", DEFAULT_MAX_TOOL_ITERATIONS)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("config: agent.max_tool_iterations musi byc liczba") from exc
        if max_tool_iterations < 1:
            raise ValueError("config: agent.max_tool_iterations musi byc >= 1")

        try:
            force_submit_in_last_n = int(
                agent_cfg.get("force_submit_in_last_n", DEFAULT_FORCE_SUBMIT_IN_LAST_N)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("config: agent.force_submit_in_last_n musi byc liczba") from exc
        if force_submit_in_last_n < 0 or force_submit_in_last_n > max_tool_iterations:
            raise ValueError(
                "config: agent.force_submit_in_last_n musi byc w [0, max_tool_iterations]"
            )

        try:
            budget_hint_last_n = int(
                agent_cfg.get("budget_hint_last_n", DEFAULT_BUDGET_HINT_LAST_N)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("config: agent.budget_hint_last_n musi byc liczba") from exc
        if budget_hint_last_n < 0 or budget_hint_last_n > max_tool_iterations:
            raise ValueError(
                "config: agent.budget_hint_last_n musi byc w [0, max_tool_iterations]"
            )

        project_name = str(
            agent_cfg.get("project_name") or Path(project_repo).name or DEFAULT_PROJECT_NAME_FALLBACK
        )

        vault_commit_prefix_raw = agent_cfg.get("vault_commit_prefix")
        if vault_commit_prefix_raw is None:
            vault_commit_prefix = DEFAULT_VAULT_COMMIT_PREFIX
        else:
            if not isinstance(vault_commit_prefix_raw, str) or not vault_commit_prefix_raw:
                raise ValueError(
                    "config: agent.vault_commit_prefix musi byc niepustym stringiem"
                )
            vault_commit_prefix = vault_commit_prefix_raw

        vault_cfg = cfg.get("vault") or {}
        if not isinstance(vault_cfg, dict):
            raise ValueError("config: sekcja 'vault' musi byc mapa")
        index_filename_raw = vault_cfg.get("index_filename")
        if index_filename_raw is None:
            vault_index_filename = DEFAULT_VAULT_INDEX_FILENAME
        else:
            if not isinstance(index_filename_raw, str) or not index_filename_raw.strip():
                raise ValueError(
                    "config: vault.index_filename musi byc niepustym stringiem"
                )
            vault_index_filename = index_filename_raw.strip()

        from src.vault.moc import DEFAULT_MOC_PATTERN as _VAULT_DEFAULT_MOC_PATTERN

        moc_pattern_raw = vault_cfg.get("moc_pattern")
        if moc_pattern_raw is None:
            vault_moc_pattern = _VAULT_DEFAULT_MOC_PATTERN
        else:
            if not isinstance(moc_pattern_raw, str) or not moc_pattern_raw.strip():
                raise ValueError(
                    "config: vault.moc_pattern musi byc niepustym stringiem"
                )
            vault_moc_pattern = moc_pattern_raw.strip()

        bootstrap_cfg_raw = vault_cfg.get("bootstrap_moc")
        if bootstrap_cfg_raw is None:
            bootstrap_moc_enabled = DEFAULT_BOOTSTRAP_MOC_ENABLED
            bootstrap_moc_name = DEFAULT_BOOTSTRAP_MOC_NAME
            bootstrap_moc_title: str | None = None
        else:
            if not isinstance(bootstrap_cfg_raw, dict):
                raise ValueError("config: vault.bootstrap_moc musi byc mapa")
            bootstrap_moc_enabled = bool(
                bootstrap_cfg_raw.get("enabled", DEFAULT_BOOTSTRAP_MOC_ENABLED)
            )
            name_raw = bootstrap_cfg_raw.get("name", DEFAULT_BOOTSTRAP_MOC_NAME)
            if not isinstance(name_raw, str) or not name_raw.strip():
                raise ValueError(
                    "config: vault.bootstrap_moc.name musi byc niepustym stringiem"
                )
            if any(ch.isspace() for ch in name_raw.strip()):
                raise ValueError(
                    "config: vault.bootstrap_moc.name nie moze zawierac whitespace"
                )
            bootstrap_moc_name = name_raw.strip()
            title_raw = bootstrap_cfg_raw.get("title")
            if title_raw is None:
                bootstrap_moc_title = None
            elif isinstance(title_raw, str) and title_raw.strip():
                bootstrap_moc_title = title_raw.strip()
            else:
                raise ValueError(
                    "config: vault.bootstrap_moc.title musi byc niepustym stringiem "
                    "albo nieobecne"
                )

        config = AgentConfig(
            config_path=resolved,
            project_repo_path=Path(project_repo).expanduser().resolve(),
            vault_path=Path(vault).expanduser().resolve(),
            language=language,
            max_retries=max_retries,
            default_commits=default_commits,
            project_name=project_name,
            vault_commit_prefix=vault_commit_prefix,
            vault_index_filename=vault_index_filename,
            vault_moc_pattern=vault_moc_pattern,
            max_tool_iterations=max_tool_iterations,
            force_submit_in_last_n=force_submit_in_last_n,
            budget_hint_last_n=budget_hint_last_n,
            bootstrap_moc_enabled=bootstrap_moc_enabled,
            bootstrap_moc_name=bootstrap_moc_name,
            bootstrap_moc_title=bootstrap_moc_title,
        )

        provider = build_provider(resolved, run_logger=run_logger)
        mcp_settings = McpSettings.from_config(resolved)

        registry = ToolRegistry()
        _register_default_tools(registry)

        return cls(
            config=config,
            provider=provider,
            git_project_reader=GitReader(config.project_repo_path),
            git_vault_reader=GitReader(config.vault_path),
            git_project_syncer=GitSyncer(config.project_repo_path),
            git_vault_syncer=GitSyncer(config.vault_path),
            vault_manager=VaultManager(config.vault_path),
            git_context_builder=GitContextBuilder.from_config(resolved),
            state_store=AgentStateStore.from_config(resolved),
            chunk_cache=ChunkCache.from_config(resolved),
            tool_registry=registry,
            mcp_settings=mcp_settings,
            run_logger=run_logger,
        )

    def sync_repos(self) -> None:
        """Pull + auto-stash obu repozytoriow (project + vault).

        Rzuca oryginalne ``GitSyncError`` / ``NoRemoteError`` / ``OfflineError``
        / ``PullConflictError`` / ``StashError`` \u2014 decyzja co zrobic
        (retry, abort, user prompt) nalezy do petli w ``main.py``.
        """

        self.git_project_syncer.sync()
        self.git_vault_syncer.sync()

    def ensure_bootstrap_moc(self) -> "BootstrapMocOutcome | None":
        """Idempotentnie zapewnia ze root-MOC vaulta istnieje (single-shot bootstrap).

        Rozwiazuje problem "pusty/mlody vault - wszystkie notatki maja
        `parent: [[MOC___Kompendium]]` wskazujacy na nieistniejacy plik".
        Po pierwszym biegu plik jest na dysku i dalsze biegi daja
        `already_present` bez zmian.

        Zachowanie:

        - Jesli ``agent.config.bootstrap_moc_enabled`` = False -> zwraca ``None``.
        - Jesli plik juz istnieje jako MOC -> ``already_present``, bez commita.
        - Jesli plik istnieje ale NIE jest MOC-iem (user ma cos swojego) ->
          ``is_not_a_moc``, bez commita ani nadpisywania.
        - Jesli plik nie istnieje -> tworzy go, commituje jeden raz z
          wiadomoscia ``'Agent: bootstrap <path>'``.

        Bootstrap commituje **bezposrednio** (bez pending/preview) bo:
        1) tresc jest deterministyczna (render_bootstrap_moc),
        2) bez niego wszystkie kolejne notatki maja `parent` -> dead link,
        3) zachowanie jest idempotentne - drugi bieg nic nie zrobi.

        Zwraca ``BootstrapMocOutcome`` gdy bootstrap byl wykonany (created
        albo already_present/is_not_a_moc), ``None`` gdy wylaczone.
        """

        if not self.config.bootstrap_moc_enabled:
            return None

        from src.vault.moc import MOCManager

        moc_manager = MOCManager(
            self.vault_manager,
            moc_pattern=self.config.vault_moc_pattern,
        )
        outcome = moc_manager.ensure_bootstrap_moc(
            name=self.config.bootstrap_moc_name,
            title=self.config.bootstrap_moc_title,
            language=self.config.language,
        )

        if outcome.result not in ("created", "merged"):
            return outcome

        try:
            repo = Repo(self.config.vault_path)
            abs_path = str((self.config.vault_path / outcome.path).resolve())
            repo.index.add([abs_path])
            verb = "bootstrap" if outcome.result == "created" else "rebuild"
            count = len(outcome.added_links)
            subject = (
                f"{DEFAULT_BOOTSTRAP_MOC_COMMIT_PREFIX}{outcome.path}"
                if outcome.result == "created"
                else f"Agent: rebuild {outcome.path} (+{count} linkow)"
            )
            body_lines: list[str] = []
            if outcome.added_links:
                body_lines.append(f"Dopisane wpisy ({count}):")
                for line in outcome.added_links:
                    body_lines.append(f"- {line}")
            message = subject if not body_lines else f"{subject}\n\n" + "\n".join(body_lines)

            author = Actor("obsidian-doc-agent", "agent@local")
            commit = repo.index.commit(message, author=author, committer=author)
            logger.info(
                "ensure_bootstrap_moc: %s %s i zacommitowany (%s, +%d linkow)",
                outcome.path, verb, commit.hexsha[:7], count,
            )
            if self.run_logger is not None:
                try:
                    self.run_logger.log(
                        f"bootstrap_moc_{outcome.result}: {outcome.path}",
                        level="info",
                        path=outcome.path,
                        vault_sha=commit.hexsha,
                        added_links_count=count,
                    )
                except Exception:  # pragma: no cover - nie blokuj bootstrapa
                    pass
        except GitCommandError as exc:
            raise RuntimeError(
                f"git add/commit bootstrap MOC sie nie udal: {exc}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"bootstrap MOC: commit sie nie udal ({type(exc).__name__}): {exc}"
            ) from exc

        return outcome

    def load_state(self) -> AgentState:
        """Wczytuje state z ``.agent-state.json`` albo tworzy nowy (pierwszy start).

        Kontrakt: brak pliku = ``AgentState()`` z pustymi listami
        processed_commits. Petla w ``main.py`` moze sie na tym oprzec
        \u2014 pierwszy start podlega regule ``agent.default_commits``.
        """

        state = self.state_store.load()
        if state is not None:
            return state
        return AgentState(processed_commits={"project": [], "vault": []})

    def save_state(self, state: AgentState) -> None:
        """Atomowy zapis state. ``state.touch()`` ustawia last_run na teraz UTC."""

        state.touch()
        self.state_store.save(state)

    def get_pending_project_commits(self, state: AgentState) -> list[CommitInfo]:
        """Zwraca liste commitow projektu do przetworzenia \u2014 od najstarszego.

        Logika:

        - pierwszy start (``processed_commits["project"]`` puste)
          \u2192 ostatnie N commitow (N = ``agent.default_commits``)
          posortowane od najstarszego.
        - kolejny bieg \u2192 ``get_commits_since_last_run(processed)``
          posortowane od najstarszego.

        Sortowanie od najstarszego jest **wymaganiem petli iteracyjnej**:
        dokumentacja musi powstawac w kolejnosci commitow, bo commit
        N+1 moze budowac na N.
        """

        processed = list(state.processed_commits.get("project", []))

        if not processed:
            commits = self.git_project_reader.get_recent_commits(
                since=None, limit=self.config.default_commits
            )
        else:
            commits = self.git_project_reader.get_commits_since_last_run(processed)

        commits.sort(key=lambda c: c.date)
        return commits

    def get_next_pending_commit(self, state: AgentState) -> CommitInfo | None:
        """Zwraca **najstarszy** nieprzetworzony commit projektu albo ``None``.

        Wygodna metoda dla petli w ``main.py`` \u2014 zamiast samemu
        zarzadzac lista, woamy ``get_next_pending_commit`` w kazdej
        iteracji i konczymy gdy ``None``.

        Uwaga: state zmienia sie po kazdej iteracji (``mark_processed``)
        i ``get_commits_since_last_run`` honoruje processed_shas \u2014 wiec
        po zapisaniu commita ``C`` do processed, kolejne wolanie tej
        metody zwroci nastepny nie-``C``.
        """

        pending = self.get_pending_project_commits(state)
        return pending[0] if pending else None

    def prepare_commit_for_ai(self, commit: CommitInfo) -> ChunkedCommit:
        """Dociaga diffy i chunkuje commit \u2014 zwraca ``ChunkedCommit`` gotowy do AI.

        ``GitReader.get_recent_commits`` zwraca lekki ``CommitInfo`` (metadata
        + lista ``FileChange`` bez ``diff_text``). Tutaj:

        1. Dociagamy pelne diffy przez ``GitReader.get_commit_diff(sha)``.
        2. Karmimy je w ``GitContextBuilder.prepare_commit`` \u2014 ktory
           **chunkuje per hunk** (nic nie ucina!) i filtruje ignore_patterns.
        3. Zwracamy ``ChunkedCommit`` z lista ``DiffChunk`` gotowa do
           multi-turn summary + finalize (albo jednego requesta dla malych).
        """

        changes_with_diff = self.git_project_reader.get_commit_diff(commit.sha)
        enriched = commit.model_copy(update={"changes": changes_with_diff})
        return self.git_context_builder.prepare_commit(enriched)

    def collect_vault_changes(self, state: AgentState) -> tuple[list[CommitInfo], list[VaultNote]]:
        """Zbiera zmiany recznie zrobione w vaulcie od ostatniego biegu.

        Zwraca parę:

        - ``commits``: lista ``CommitInfo`` z repo vaulta, **odfiltrowana**
          z commitow wykonanych przez agenta (heurystyka: wiadomosc
          zaczyna sie od ``"Agent: sync z "``) \u2014 zeby AI nie widzialo
          wlasnych commitow jako "zmian usera".
        - ``notes``: aktualne tresci (``VaultNote``) notatek, ktore
          wystapily jako zmienione w tych commitach. Duplikaty
          usuwane po sciezce, czytanie przez ``VaultManager.read_note``
          (zwraca najnowsza tresc, nie diff).
        """

        processed_vault = list(state.processed_commits.get("vault", []))
        raw_commits = self.git_vault_reader.get_commits_since_last_run(processed_vault)

        user_commits = [
            c for c in raw_commits
            if not c.message.strip().startswith(self.config.vault_commit_prefix)
        ]
        user_commits.sort(key=lambda c: c.date)

        seen_paths: set[str] = set()
        changed_paths: list[str] = []
        for commit in user_commits:
            for change in commit.changes:
                if not change.path.endswith(".md"):
                    continue
                if change.path in seen_paths:
                    continue
                seen_paths.add(change.path)
                changed_paths.append(change.path)

        notes: list[VaultNote] = []
        for rel_path in changed_paths:
            try:
                if self.vault_manager.note_exists(rel_path):
                    notes.append(self.vault_manager.read_note(rel_path))
            except Exception:
                logger.exception("Nie udalo sie wczytac zmienionej notatki %s", rel_path)

        return user_commits, notes

    def scan_vault(self) -> VaultKnowledge:
        """Pelny skan vaulta \u2014 buduje ``VaultKnowledge`` dla promptu i MOC plannera."""

        return self.vault_manager.scan_all()

    async def propose_actions(
        self,
        *,
        chunked_commit: ChunkedCommit,
        vault_changes: list[CommitInfo],
        vault_changed_notes: list[VaultNote],
        vault_knowledge: VaultKnowledge,
        on_chunk_progress: "ChunkProgressCallback | None" = None,
    ) -> ProposedPlan:
        """Backward-compat wrapper na ``run_session`` \u2014 zwraca sam ``ProposedPlan``.

        Nowy kod powinien uzywac ``run_session``, ktore zwraca pelny
        ``SessionResult`` z metrykami (iterations_used, tool_calls_count,
        finalized_by_submit_plan).

        Dwie sciezki (decyzja user: `delivery=summarize_first`):

        - **Small** (``chunked_commit.is_small()``): wszystkie chunki
          miesza sie w jednym requescie. System prompt + petla tool-use
          z narzedziami vault_write + eksploracja + terminator ``submit_plan``.
        - **Chunked**: iterujemy po chunkach, dla kazdego zapytanie AI
          o 3-6 zdaniowe podsumowanie (cache-owane per ``(sha, path,
          chunk_idx)`` w ``ChunkCache``). Potem FINALIZE: analogiczna petla
          tool-use na zagregowanych podsumowaniach + ``submit_plan``.
        """

        session = await self._run_proposal_session(
            chunked_commit=chunked_commit,
            vault_changes=vault_changes,
            vault_changed_notes=vault_changed_notes,
            vault_knowledge=vault_knowledge,
            on_chunk_progress=on_chunk_progress,
        )
        return session.plan

    async def _run_proposal_session(
        self,
        *,
        chunked_commit: ChunkedCommit,
        vault_changes: list[CommitInfo],
        vault_changed_notes: list[VaultNote],
        vault_knowledge: VaultKnowledge,
        on_chunk_progress: "ChunkProgressCallback | None",
    ) -> SessionResult:
        """Wewnetrzny entrypoint zwracajacy pelny ``SessionResult`` z metrykami."""

        templates = self._templates()

        if chunked_commit.is_small():
            return await self._propose_small(
                chunked_commit=chunked_commit,
                vault_changes=vault_changes,
                vault_changed_notes=vault_changed_notes,
                vault_knowledge=vault_knowledge,
                templates=templates,
            )

        return await self._propose_chunked(
            chunked_commit=chunked_commit,
            vault_changes=vault_changes,
            vault_changed_notes=vault_changed_notes,
            vault_knowledge=vault_knowledge,
            templates=templates,
            on_chunk_progress=on_chunk_progress,
        )

    async def _propose_small(
        self,
        *,
        chunked_commit: ChunkedCommit,
        vault_changes: list[CommitInfo],
        vault_changed_notes: list[VaultNote],
        vault_knowledge: VaultKnowledge,
        templates: dict[str, str],
    ) -> SessionResult:
        """SMALL commit: system + user prompt -> petla tool-use (Faza 2 refaktoru).

        Model woła narzedzia vault_write i konczy sesje ``submit_plan``.
        Agent zbiera propozycje z ``ctx.proposed_writes`` i zwraca
        ``ProposedPlan`` kompatybilny z ``apply_pending`` flow.

        Retry walidacji (``max_retries``): aktualna petla tool-use moze sie
        skonczyc bez ``submit_plan`` (watchdog ``max_tool_iterations``)
        albo z pustym ``proposed_writes`` i bez ``final_summary`` — w takim
        wypadku zgłaszamy ``_ProposedPlanValidationError`` i powtarzamy
        sesje w kolejnej iteracji retry.
        """

        user_prompt_builder = lambda retry_error, previous_actions: build_user_prompt(  # noqa: E731
            chunked_commit=chunked_commit,
            vault_changes=vault_changes,
            vault_changed_notes=vault_changed_notes,
            vault_knowledge=vault_knowledge,
            templates=templates,
            project_name=self.config.project_name,
            retry_error=retry_error,
            previous_actions=previous_actions,
        )
        return await self._propose_with_tool_loop(
            phase="SMALL",
            commit_info=chunked_commit.commit,
            extra_system_prompts=[],
            user_prompt_builder=user_prompt_builder,
        )

    async def _propose_chunked(
        self,
        *,
        chunked_commit: ChunkedCommit,
        vault_changes: list[CommitInfo],
        vault_changed_notes: list[VaultNote],
        vault_knowledge: VaultKnowledge,
        templates: dict[str, str],
        on_chunk_progress: "ChunkProgressCallback | None",
    ) -> SessionResult:
        """Multi-turn: podsumuj kazdy chunk (z cache) \u2192 FINALIZE z submit_plan."""

        commit_sha = chunked_commit.commit.sha
        total_chunks = chunked_commit.total_chunks

        summaries: list[ChunkSummary] = []
        for idx, chunk in enumerate(chunked_commit.chunks, start=1):
            cached = self.chunk_cache.get_summary(commit_sha, chunk)
            if cached is not None:
                logger.info(
                    "Chunk cache hit: sha=%s idx=%d/%d pliki=%s",
                    commit_sha[:7], chunk.chunk_idx, chunk.total_chunks,
                    ",".join(chunk.file_paths),
                )
                summaries.append(cached)
                if on_chunk_progress is not None:
                    on_chunk_progress(idx, total_chunks, chunk, True)
                continue

            summary = await self._ask_chunk_summary(
                chunked_commit=chunked_commit,
                chunk=chunk,
                global_position=(idx, total_chunks),
            )
            self.chunk_cache.put_summary(commit_sha, chunk, summary)
            self.chunk_cache.put_chunk(commit_sha, chunk)
            summaries.append(summary)
            if on_chunk_progress is not None:
                on_chunk_progress(idx, total_chunks, chunk, False)

        return await self._ask_finalize(
            chunked_commit=chunked_commit,
            chunk_summaries=summaries,
            vault_changes=vault_changes,
            vault_changed_notes=vault_changed_notes,
            vault_knowledge=vault_knowledge,
            templates=templates,
        )

    async def _ask_chunk_summary(
        self,
        *,
        chunked_commit: ChunkedCommit,
        chunk: DiffChunk,
        global_position: tuple[int, int],
    ) -> ChunkSummary:
        """Wola AI o podsumowanie JEDNEGO chunka. Zwraca ``ChunkSummary``.

        Bez tool callingu \u2014 AI odpowiada zwyklym tekstem (3-6 zdan).
        Bez retry walidacji \u2014 nie ma schematu do spelnienia (tylko
        sanity check: niepusty tekst).
        """

        system_prompt = self._chunk_instruction_prompt()
        user_prompt = build_chunk_summary_prompt(
            chunked_commit=chunked_commit,
            chunk=chunk,
            chunk_position=global_position,
            project_name=self.config.project_name,
        )

        request = ChatRequest(
            messages=[
                ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
                ChatMessage(role=MessageRole.USER, content=user_prompt),
            ],
            tools=[],
            tool_choice=None,
        )

        call_ctx = LLMCallContext(
            phase="CHUNK_SUMMARY",
            commit_sha=chunked_commit.commit.sha,
            chunk_idx=chunk.chunk_idx,
            chunk_total=chunk.total_chunks,
            chunk_id=chunk.chunk_id,
            files=tuple(chunk.file_paths),
        )
        try:
            with llm_call_context(call_ctx):
                result = await self.provider.complete(request)
        except Exception as exc:
            files_dbg = ",".join(chunk.file_paths) or "(none)"
            raise RuntimeError(
                f"Blad wywolania providera {self.provider.name} przy chunk-summary "
                f"(sha={chunked_commit.commit.sha[:7]}, chunk={chunk.chunk_idx}/{chunk.total_chunks}, "
                f"pliki=[{files_dbg}]): {exc}"
            ) from exc

        summary_text = (result.text or "").strip()
        if not summary_text:
            files_dbg = ",".join(chunk.file_paths) or "(none)"
            raise RuntimeError(
                f"AI zwrocilo puste chunk-summary dla chunk {chunk.chunk_idx}/{chunk.total_chunks} "
                f"(pliki=[{files_dbg}]). Provider: {self.provider.name}, model: {result.model}."
            )

        return ChunkSummary(
            chunk_idx=chunk.chunk_idx,
            total_chunks=chunk.total_chunks,
            summary=summary_text,
            model=result.model,
            file_paths=list(chunk.file_paths),
        )

    async def _ask_finalize(
        self,
        *,
        chunked_commit: ChunkedCommit,
        chunk_summaries: list[ChunkSummary],
        vault_changes: list[CommitInfo],
        vault_changed_notes: list[VaultNote],
        vault_knowledge: VaultKnowledge,
        templates: dict[str, str],
    ) -> SessionResult:
        """FINALIZE (chunked): podsumowania chunkow + finalize_prompt -> petla tool-use.

        Tak samo jak SMALL, ale user prompt jest zbudowany z agregatu podsumowan
        chunkow (nie z pelnych diffow). System prompt + finalize_prompt dolaczone.
        """

        user_prompt_builder = lambda retry_error, previous_actions: build_finalize_prompt(  # noqa: E731
            chunked_commit=chunked_commit,
            chunk_summaries=chunk_summaries,
            vault_changes=vault_changes,
            vault_changed_notes=vault_changed_notes,
            vault_knowledge=vault_knowledge,
            templates=templates,
            project_name=self.config.project_name,
            retry_error=retry_error,
            previous_actions=previous_actions,
        )
        return await self._propose_with_tool_loop(
            phase="FINALIZE",
            commit_info=chunked_commit.commit,
            extra_system_prompts=[self._finalize_prompt()],
            user_prompt_builder=user_prompt_builder,
        )

    async def _propose_with_tool_loop(
        self,
        *,
        phase: str,
        commit_info: CommitInfo,
        extra_system_prompts: list[str],
        user_prompt_builder: "Callable[[str | None, list[dict[str, Any]] | None], str]",
    ) -> SessionResult:
        """Wspolne ciało SMALL / FINALIZE: retry-owana petla tool-use.

        Zwraca ``SessionResult`` z metrykami (iterations_used, tool_calls_count)
        i ``ProposedPlan`` kompatybilnym z ``apply_pending``/``finalize_pending``.
        Retry w przypadku:

        - ``max_tool_iterations`` wyczerpane bez ``submit_plan``
        - model nie zaproponowal zadnej akcji write i nie wolal ``submit_plan``
          (fallback exit) - dla Fazy 2 to blad walidacji
        - budowa ``ProposedPlan`` padnie (niepuste summary itd.)

        Przy retry przekazujemy do ``user_prompt_builder`` **snapshot akcji
        z poprzedniej proby** (``exc.executed_actions``) - dzieki temu model
        w kolejnej iteracji wie, co juz sam zrobil w poprzednim podejsciu
        (ktore poszlo do kosza bo ctx jest swiezy), i nie zaczyna od eksploracji
        od zera. Zwykle to oznacza: "juz zarejestrowales create_changelog_entry
        i replace_section w poprzedniej probie, teraz tylko submit_plan".
        """

        last_exc: Exception | None = None
        retry_error: str | None = None
        previous_actions: list[dict[str, Any]] | None = None

        for attempt in range(self.config.max_retries + 1):
            user_prompt = user_prompt_builder(retry_error, previous_actions)

            try:
                session_result = await self._run_tool_loop(
                    phase=phase,
                    commit_info=commit_info,
                    extra_system_prompts=extra_system_prompts,
                    user_prompt=user_prompt,
                    attempt=attempt + 1,
                )
            except _ProposedPlanValidationError as exc:
                last_exc = exc
                retry_error = str(exc)
                previous_actions = list(exc.executed_actions) if exc.executed_actions else None
                logger.warning(
                    "%s attempt %d/%d: tool loop walidacja padla: %s "
                    "(przenosze %d akcji z poprzedniej proby do retry promptu)",
                    phase, attempt + 1, self.config.max_retries + 1, exc,
                    len(previous_actions) if previous_actions else 0,
                )
                continue
            except Exception as exc:
                raise RuntimeError(
                    f"Blad wywolania providera {self.provider.name} przy {phase}: {exc}"
                ) from exc

            return session_result

        raise RuntimeError(
            f"{phase}: AI nie zwrocilo poprawnej odpowiedzi po "
            f"{self.config.max_retries + 1} probach. Ostatni blad: {last_exc}"
        )

    async def _run_tool_loop(
        self,
        *,
        phase: str,
        commit_info: CommitInfo,
        extra_system_prompts: list[str],
        user_prompt: str,
        attempt: int,
    ) -> SessionResult:
        """Glowna petla tool-use (Faza 7 refaktoru agentic tool loop).

        ::

            ┌────── iteracja 1..N ──────┐
            │ provider.complete(         │
            │    messages,                │  ← system+user+history
            │    tools=list_tools(),      │  ← MCP cached
            │    tool_choice="auto")      │
            │         │                   │
            │         ▼                   │
            │  result.tool_calls?         │
            │    ├─ yes → dispatch_mcp    │  ← call_tool(name,args)
            │    │         each,          │
            │    │         append TOOL    │
            │    │         message        │
            │    └─ no → exit (validate)  │
            │         │                   │
            │         ▼                   │
            │   ctx.finalized?            │  ← submit_plan was called
            │    ├─ yes → build plan      │
            │    └─ no → continue loop    │
            └─────────────────────────────┘

        1. Buduje swiezy ``ToolExecutionContext`` dla tej sesji (per commit).
        2. Pobiera liste narzedzi z ``McpAgentClient.list_tools`` (cache).
        3. Iteruje max ``config.max_tool_iterations`` razy (pelny lifecycle
           opisany w ASCII wyzej).
        4. Buduje ``SessionResult`` z ``ProposedPlan`` + metrykami.

        **Rzuca ``_ProposedPlanValidationError``** gdy:

        - petla wyszla bez ``submit_plan`` (nie ma final_summary);
        - budowa ``ProposedPlan`` padla (puste summary, zle sciezki itp.);
        - model nie wolal zadnych narzedzi w pierwszej iteracji.

        Te bledy sa retryowalne — wyzsza warstwa (``_propose_with_tool_loop``)
        moze sprobowac jeszcze raz z ``retry_error`` w prompcie.
        """

        if self._mcp_client is None or not self._mcp_client.connected:
            raise RuntimeError(
                "MCP client nie jest polaczony - wywolaj agent.start_mcp() przed run_session."
            )

        ctx = ToolExecutionContext(
            vault_manager=self.vault_manager,
            git_reader=self.git_project_reader,
            commit_info=commit_info,
            run_logger=self.run_logger,
        )
        self.set_tool_ctx(ctx)

        try:
            tool_definitions = await self._mcp_client.list_tools()
            if not tool_definitions:
                raise _ProposedPlanValidationError(
                    "MCP nie wystawia zadnych narzedzi - sprawdz _register_default_tools.",
                    executed_actions=list(ctx.executed_actions),
                )

            system_prompt = self._system_prompt()
            messages: list[ChatMessage] = [
                ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
            ]
            for extra in extra_system_prompts:
                messages.append(ChatMessage(role=MessageRole.SYSTEM, content=extra))
            messages.append(ChatMessage(role=MessageRole.USER, content=user_prompt))

            tool_calls_total = 0
            iterations_used = 0
            max_iter = self.config.max_tool_iterations
            force_last_n = self.config.force_submit_in_last_n
            hint_last_n = self.config.budget_hint_last_n

            for iteration in range(1, max_iter + 1):
                iterations_used = iteration
                iterations_left = max_iter - iteration

                # Wymuszenie terminatora na ostatnich N iteracjach: model dostaje
                # `tool_choice={"type":"tool","name":"submit_plan"}` i nie moze
                # wybrac niczego innego. Chroni przed klasyczna patologia
                # "model eksploruje do konca budzetu i nie finalizuje sesji".
                tool_choice_value: str | dict[str, Any] = "auto"
                if force_last_n > 0 and iterations_left < force_last_n:
                    tool_choice_value = {"type": "tool", "name": "submit_plan"}

                call_ctx = LLMCallContext(
                    phase=phase,
                    commit_sha=commit_info.sha,
                    attempt=attempt,
                    files=tuple(c.path for c in commit_info.changes),
                    iteration=iteration,
                )
                request = ChatRequest(
                    messages=messages,
                    tools=tool_definitions,
                    tool_choice=tool_choice_value,
                    parallel_tool_calls=True,
                )

                with llm_call_context(call_ctx):
                    result = await self.provider.complete(request)

                tool_calls_this_turn = len(result.tool_calls)
                if self.run_logger is not None:
                    self.run_logger.log_tool_loop_iteration(
                        sha=commit_info.sha,
                        iteration=iteration,
                        max_iterations=max_iter,
                        tool_calls=tool_calls_this_turn,
                    )

                messages.append(
                    ChatMessage(
                        role=MessageRole.ASSISTANT,
                        content=result.text or None,
                        tool_calls=list(result.tool_calls),
                    )
                )

                if not result.tool_calls:
                    if iteration == 1:
                        raise _ProposedPlanValidationError(
                            "Model nie wywolal zadnego narzedzia - wywolaj narzedzia "
                            "vault_write (create_hub/create_concept/..., append_section, "
                            "add_moc_link, ...) i zakoncz submit_plan.",
                            executed_actions=list(ctx.executed_actions),
                        )
                    break

                # Czy do tool_result dokleic ostrzezenie o budzecie: tylko jak
                # faktycznie zblizamy sie do limitu. Dopisek idzie jako suffix
                # content, a NIE jako nowy system message - dzieki temu nie
                # rozbija prefiksu prompt cache'a.
                budget_hint_suffix = ""
                if hint_last_n > 0 and iterations_left < hint_last_n:
                    budget_hint_suffix = self._format_budget_hint(
                        iteration=iteration,
                        max_iter=max_iter,
                        iterations_left=iterations_left,
                        force_last_n=force_last_n,
                    )

                # Rownolegly dispatch tool_calls z jednej tury modelu.
                # Model (przy `parallel_tool_calls=True`) moze wyemitowac wiele
                # tool_use w jednej odpowiedzi (np. 8x create_module). Dispatchujemy
                # je przez asyncio.gather, zeby I/O do MCP (HTTP streamable) leciało
                # wspolbieznie - zamiast sumy latencji dostajemy max z latencji.
                #
                # Kolejnosc tool_result w `messages` MUSI odpowiadac kolejnosci
                # tool_use w odpowiedzi asystenta (wymog Anthropic/OpenAI tool
                # protocol). Uzywamy `zip(result.tool_calls, results)` po gather -
                # to daje deterministyczne ulozenie niezaleznie od tego, ktore
                # narzedzie skonczylo pierwsze.
                #
                # Race na `ctx` (proposed_writes.append, invalidate_vault_knowledge):
                # wszystkie dispatch'e zyja w jednym event loop (asyncio jest
                # single-thread), a kazda operacja na liscie/atrybucie jest
                # atomowa w obrebie pojedynczego kroku awaitowego. Niedeterminizm
                # dotyczy tylko *wzglednej* kolejnosci appendow pomiedzy
                # narzedziami biegnacymi rownoczesnie w tej turze - gdy sa to
                # operacje na roznych plikach (typowy batch create_module na
                # nieistniejacych modulach), kolejnosc nie ma znaczenia.
                # Gdy beda na tym samym pliku (np. create_X + append_section(X)),
                # model sam powinien rozbic na dwie tury (bo append wymaga
                # create juz w `ctx.has_pending_create`), a nawet gdyby tego
                # nie zrobil, `apply_pending` aplikuje sekwencyjnie w kolejnosci
                # w jakiej sa w liscie - ewentualne przestawienie zglosi blad
                # przez preconditions (FileNotFoundError przy append).
                tool_calls_total += len(result.tool_calls)
                dispatch_coros = [
                    self._dispatch_via_mcp(tool_call) for tool_call in result.tool_calls
                ]
                dispatch_results = await asyncio.gather(*dispatch_coros)

                for tool_call, tool_result in zip(result.tool_calls, dispatch_results):
                    content = tool_result.to_model_text()
                    if budget_hint_suffix:
                        content = f"{content}{budget_hint_suffix}"
                    messages.append(
                        ChatMessage(
                            role=MessageRole.TOOL,
                            tool_call_id=tool_call.id,
                            content=content,
                        )
                    )

                if ctx.finalized:
                    break
            else:
                if self.run_logger is not None:
                    self.run_logger.log_tool_loop_exhausted(
                        sha=commit_info.sha,
                        max_iterations=max_iter,
                        tool_calls_total=tool_calls_total,
                        proposed_writes=len(ctx.proposed_writes),
                        reason="max_iterations",
                    )
                raise _ProposedPlanValidationError(
                    f"Przekroczono max_tool_iterations={max_iter} "
                    f"bez wywolania submit_plan. Zakoncz sesje submit_plan.",
                    executed_actions=list(ctx.executed_actions),
                )

            if not ctx.finalized:
                if self.run_logger is not None:
                    self.run_logger.log_tool_loop_exhausted(
                        sha=commit_info.sha,
                        max_iterations=max_iter,
                        tool_calls_total=tool_calls_total,
                        proposed_writes=len(ctx.proposed_writes),
                        reason="no_tool_calls",
                    )
                raise _ProposedPlanValidationError(
                    "Model przestal wolac narzedzia bez wywolania submit_plan. "
                    "Ostatnim krokiem musi byc submit_plan.",
                    executed_actions=list(ctx.executed_actions),
                )

            plan = self._build_proposed_plan(ctx)

            if self.run_logger is not None:
                self.run_logger.log_tool_loop_finalized(
                    sha=commit_info.sha,
                    iterations_used=iterations_used,
                    tool_calls_total=tool_calls_total,
                    proposed_writes=len(ctx.proposed_writes),
                )

            return SessionResult(
                plan=plan,
                iterations_used=iterations_used,
                tool_calls_count=tool_calls_total,
                finalized_by_submit_plan=True,
            )
        finally:
            self.clear_tool_ctx()

    async def _dispatch_via_mcp(self, tool_call: "ToolCall") -> "ToolResult":
        """Woła narzedzie po stronie MCP + wstrzykuje argumenty JSON → dict.

        Delegacja do ``McpAgentClient.call_tool``. Wszystkie bledy sa mapowane na
        ``ToolResult(ok=False)`` - nic nie wycieka jako wyjatek (model dostaje
        tekst "ERROR: ..." i moze sie poprawic).
        """

        import json as _json

        assert self._mcp_client is not None
        raw_args = tool_call.function.arguments or ""
        args: dict[str, Any]
        if not raw_args.strip():
            args = {}
        else:
            try:
                parsed = _json.loads(raw_args)
            except _json.JSONDecodeError as exc:
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

        return await self._mcp_client.call_tool(tool_call.function.name, args)

    def _build_proposed_plan(self, ctx: ToolExecutionContext) -> ProposedPlan:
        """Konwertuje ``ctx.proposed_writes`` + ``ctx.final_summary`` na ``ProposedPlan``.

        Walidacja Pydantic ``ProposedPlan`` sprawdzi niepuste summary i format
        pisow. Blad walidacji jest opakowany w ``_ProposedPlanValidationError``,
        ktory jest retryowalny wyzej.
        """

        summary = (ctx.final_summary or "").strip()
        if not summary:
            raise _ProposedPlanValidationError(
                "submit_plan nie dostarczyl niepustego summary.",
                executed_actions=list(ctx.executed_actions),
            )
        try:
            return ProposedPlan(summary=summary, writes=list(ctx.proposed_writes))
        except ValidationError as exc:
            raise _ProposedPlanValidationError(
                f"Budowa ProposedPlan z proposed_writes padla: {exc}",
                executed_actions=list(ctx.executed_actions),
            ) from exc

    @staticmethod
    def _format_budget_hint(
        *,
        iteration: int,
        max_iter: int,
        iterations_left: int,
        force_last_n: int,
    ) -> str:
        """Krotki, deterministyczny sufix doklejany do ``tool_result``.

        Idzie do content **tool_result** (nie system message), wiec Anthropic
        prompt cache prefiksu nie rozbija - tylko ogonek sie zmienia.
        Trzymamy jeden, staly format: model lapie go bez potrzeby NLP.
        """

        lines = [
            "",
            "",
            f"[budzet-petli: iteracja {iteration}/{max_iter}, "
            f"pozostalo {iterations_left}]",
        ]
        if force_last_n > 0 and iterations_left < force_last_n:
            lines.append(
                "[TWARDE WYMUSZENIE: kolejna iteracja bedzie miala tool_choice=submit_plan - "
                "nie zdazysz wywolac niczego innego. Zakoncz TERAZ przez submit_plan.]"
            )
        elif iterations_left <= 2:
            lines.append(
                "[UWAGA: zbliza sie koniec petli. Jesli masz wszystko co trzeba - wywolaj submit_plan.]"
            )
        return "\n".join(lines)

    async def run_session(
        self,
        *,
        chunked_commit: ChunkedCommit,
        vault_changes: list[CommitInfo],
        vault_changed_notes: list[VaultNote],
        vault_knowledge: VaultKnowledge,
        on_chunk_progress: "ChunkProgressCallback | None" = None,
    ) -> SessionResult:
        """Glowny publiczny entrypoint - zwraca pelny ``SessionResult``.

        Uruchamia petle tool-use (SMALL albo chunked FINALIZE) z prawdziwymi
        metrykami: liczba iteracji, liczba wywolanych tooli,
        czy zakonczono poprzez ``submit_plan``. ``session.plan`` to
        ``ProposedPlan`` zgodny z ``apply_pending``/``finalize_pending``.

        Dla starego kodu pozostaje ``propose_actions``, ktory zwraca sam
        ``ProposedPlan`` (cienki wrapper na te metode).
        """

        return await self._run_proposal_session(
            chunked_commit=chunked_commit,
            vault_changes=vault_changes,
            vault_changed_notes=vault_changed_notes,
            vault_knowledge=vault_knowledge,
            on_chunk_progress=on_chunk_progress,
        )

    def plan_post_updates(
        self,
        plan: ProposedPlan,
        knowledge: VaultKnowledge,
    ) -> list[PlannedVaultWrite]:
        """Pre-compute plany MOC i indeksu dla zaproponowanych pisow.

        W Fazie 7 ``moc_planner`` dziala tylko jako **safety net** — tj.
        dokleja brakujace wpisy MOC / ``_index.md`` dla notatek ``create``,
        ktore model pominal (nie wywolal ``add_moc_link`` i nie ustawil
        ``parent`` we frontmatterze).
        """

        if not plan.writes:
            return []
        return plan_post_action_updates(
            plan.writes,
            self.vault_manager,
            knowledge,
            index_path=self.config.vault_index_filename,
            language=self.config.language,
        )

    def execute_plan(
        self,
        plan: ProposedPlan,
        plans: list[PlannedVaultWrite],
    ) -> ActionExecutionReport:
        """Aplikuje pisy + plany na vaulcie. Best-effort, zwraca raport.

        **Legacy** — zapisuje bez podswietlenia i bez snapshotu.
        Nowy flow uzywa ``apply_pending`` + ``finalize_pending`` /
        ``rollback_pending``. Zostawione dla testow/dry-run.
        """

        return self.action_executor.execute(plan.writes, plans)

    def apply_pending(
        self,
        plan: ProposedPlan,
        plans: list[PlannedVaultWrite],
    ) -> tuple[ActionExecutionReport, PendingBatch]:
        """Zapisuje zmiany do vaulta Z ZIELONYM PODSWIETLENIEM + snapshot.

        Cienka fasada wokol ``ActionExecutor.apply_pending``. User musi
        potem przejrzec vault w Obsidianie i wybrac:

        - ``finalize_pending`` (akceptacja — usuwa zielone tlo + commit)
        - ``rollback_pending`` (odrzucenie — restore ze snapshotu, bez commita)
        """

        return self.action_executor.apply_pending(plan.writes, plans)

    def finalize_pending(self, batch: PendingBatch) -> list[str]:
        """Po akceptacji: nadpisuje pliki czysta trescia (zielone tlo znika).

        Nie commituje — commit leci osobno przez ``commit_vault``.
        Zwraca liste sciezek faktycznie rewrite-owanych (do logu).
        """

        return self.action_executor.finalize_pending(batch)

    def rollback_pending(self, batch: PendingBatch) -> list[str]:
        """Po odrzuceniu: przywraca vault do stanu ze snapshotu (akcje + plany).

        Nie commituje i nie zostawia zadnych sladow — plik,
        ktorego nie bylo przed apply, zostanie usuniety; plik ktory
        byl — dostanie z powrotem poprzednia tresc.
        """

        return self.action_executor.rollback_pending(batch)

    def commit_vault(
        self,
        *,
        approved: bool,
        project_commit: CommitInfo,
        execution_report: ActionExecutionReport,
        summary: str,
    ) -> str:
        """Robi **jeden** commit gitowy na vault obejmujacy touched_files.

        Druga linia obrony: ``approved=False`` rzuca ``RuntimeError``.
        Push nie jest wolany (pozostaje manualny).

        Commit message: ``"Agent: sync z <project_name>@<short_sha>\\n\\n<summary>"``.
        Body zawiera summary od AI oraz liste plikow, co ulatwia ewentualny
        revert i audyt.

        Zwraca SHA nowego commita vault (pelny).
        """

        if not approved:
            raise RuntimeError(
                "commit_vault: approved=False \u2014 odmowa zapisu ze strony executora. "
                "Agent nie commituje bez zgody usera."
            )

        touched = execution_report.touched_files
        if not touched:
            raise RuntimeError(
                "commit_vault: brak plikow do zacommitowania \u2014 wszystkie akcje padly. "
                "Zajrzyj do ActionExecutionReport.failed."
            )

        repo = Repo(self.config.vault_path)
        abs_paths = [str((self.config.vault_path / p).resolve()) for p in touched]

        try:
            repo.index.add(abs_paths)
        except GitCommandError as exc:
            raise RuntimeError(f"git add zwrocil blad w vaulcie: {exc}") from exc

        short_sha = project_commit.sha[:7]
        subject = f"{self.config.vault_commit_prefix}{self.config.project_name}@{short_sha}"
        files_list = "\n".join(f"- {p}" for p in touched)
        body = f"{summary}\n\nZmienione pliki:\n{files_list}"
        message = f"{subject}\n\n{body}"

        author = Actor("obsidian-doc-agent", "agent@local")
        try:
            commit = repo.index.commit(message, author=author, committer=author)
        except Exception as exc:
            raise RuntimeError(f"git commit na vaulcie sie nie udal: {exc}") from exc

        logger.info("Zacommitowano vault: %s \u2014 %s", commit.hexsha[:7], subject)
        return commit.hexsha

    def mark_commit_processed(
        self,
        state: AgentState,
        *,
        project_sha: str,
        vault_commit_sha: str | None,
    ) -> None:
        """Dopisuje commit projektowy (zawsze) i vault (jesli byl) do state.

        **Nie** zapisuje state do pliku \u2014 to robi osobno ``save_state``.
        Dzieki temu petla moze robic multiple mark + jeden zapis na koniec
        iteracji (bardziej atomowo).
        """

        state.mark_processed("project", [project_sha])
        if vault_commit_sha:
            state.mark_processed("vault", [vault_commit_sha])

    def mark_vault_user_commits_processed(
        self,
        state: AgentState,
        user_commits: list[CommitInfo],
    ) -> None:
        """Oznacza reczne commity usera w vaulcie jako uwzglednione.

        Bez tego samo ``collect_vault_changes`` w kazdym biegu wczytywalo
        by te same user commity jako "nowe zmiany usera" dopoki nie
        wypadna z okna ``GitReader.get_commits_since_last_run`` \u2014 a AI
        dostawalo by je w prompcie kazdorazowo.

        Wolamy na koncu biegu (gdy uwzglednilismy je juz w kontekscie
        co najmniej jednego wywolania AI), tuz przed ``save_state``.
        """

        shas = [c.sha for c in user_commits]
        if shas:
            state.mark_processed("vault", shas)

    def update_vault_snapshot(self, state: AgentState, knowledge: VaultKnowledge) -> None:
        """Aktualizuje ``state.vault_snapshot`` z aktualnego ``VaultKnowledge``.

        Wolane na koncu biegu, tuz przed ``save_state``.
        """

        state.vault_snapshot = VaultSnapshot.from_knowledge(knowledge)

    def _system_prompt(self) -> str:
        if self._system_prompt_cache is None:
            self._system_prompt_cache = load_system_prompt(
                self.config.language,
                examples=load_all_examples(),
            )
        return self._system_prompt_cache

    def _chunk_instruction_prompt(self) -> str:
        if self._chunk_instruction_prompt_cache is None:
            self._chunk_instruction_prompt_cache = load_chunk_instruction_prompt(self.config.language)
        return self._chunk_instruction_prompt_cache

    def _finalize_prompt(self) -> str:
        if self._finalize_prompt_cache is None:
            self._finalize_prompt_cache = load_finalize_prompt(self.config.language)
        return self._finalize_prompt_cache

    def _templates(self) -> dict[str, str]:
        if self._templates_cache is None:
            self._templates_cache = load_all_templates()
        return self._templates_cache

    # ------------------------------------------------------------------
    # MCP runtime lifecycle (Faza 1 refaktoru agentic tool loop)
    # ------------------------------------------------------------------

    @property
    def mcp_runtime(self) -> McpRuntime | None:
        """Zwraca aktywny ``McpRuntime`` albo ``None`` gdy nie wystartowany.

        Wystawione dla testow / debug. Normalny flow uzywa ``start_mcp`` /
        ``stop_mcp`` i nie dotyka runtime'u bezposrednio.
        """

        return self._mcp_runtime

    @property
    def mcp_client(self) -> McpAgentClient | None:
        """Zwraca aktywny ``McpAgentClient`` albo ``None`` gdy niepolaczony."""

        return self._mcp_client

    def attach_mcp_client(self, client: Any) -> None:
        """Podmienia klienta MCP (Faza 7 - hook dla ``InMemoryMcpTransport``).

        Pomija uruchomienie ``FastMCP`` + ``McpRuntime`` - zaklada ze
        ``client`` jest juz "polaczony" (``client.connected == True``)
        albo zostanie polaczony przed ``run_session``. Uzywane w testach
        przez ``src.agent.mcp.InMemoryMcpTransport``, ktory dispatchuje
        narzedzia wprost do ``ToolRegistry`` bez HTTP.

        Normalny produkcyjny flow uzywa ``start_mcp()`` - metoda
        ``attach_mcp_client`` jest szczegolem testowym.
        """

        self._mcp_client = client

    def _tool_ctx_provider(self) -> ToolExecutionContext:
        """Zwraca ``current`` context narzedzi albo tworzy swiezy single-use.

        Serwer MCP uzywa tego gettera przy kazdym ``call_tool`` - dzieki temu
        agent moze podmieniac context per sesja (per commit) bez restartu
        serwera. W Fazie 1 wolamy ``ensure_tool_ctx`` przed startem pierwszej
        sesji; gdyby tego nie zrobiono, provider wygeneruje domyslny context
        z samym ``vault_manager`` (safety net na zewnetrzne wywolania przez
        mcp-inspector zanim agent cokolwiek zrobi).
        """

        if self._current_tool_ctx is None:
            self._current_tool_ctx = ToolExecutionContext(vault_manager=self.vault_manager)
        return self._current_tool_ctx

    def set_tool_ctx(self, ctx: ToolExecutionContext) -> None:
        """Ustawia ``current`` context narzedzi. Wolane przez agenta przed sesja.

        Faza 2 podmieni to na automatyczne tworzenie w ``run_session``.
        W Fazie 1 nikt tego nie woluje jeszcze - serwer jest glownie po to,
        zeby zewnetrzni klienci mogli zobaczyc narzedzia (registry jest pusty).
        """

        self._current_tool_ctx = ctx

    def clear_tool_ctx(self) -> None:
        """Zerwa ``current`` context narzedzi. Pozniejsze wywolania dostaja swiezy."""

        self._current_tool_ctx = None

    async def start_mcp(self) -> None:
        """Startuje in-process MCP runtime + klient. Idempotentne.

        Po Fazie 7 MCP jest **jedyna** sciezka komunikacji z narzedziami —
        ``mcp.enabled`` musi byc ``True`` (domyslnie). Testy, ktore nie
        chca HTTP, powinny uzywac ``InMemoryMcpTransport`` zamiast
        wylaczac MCP globalnie. Rzuca ``RuntimeError`` przy kolizji portu
        lub timeoucie startu.
        """

        if not self.mcp_settings.enabled:
            raise RuntimeError(
                "mcp.enabled=false zostalo usuniete w Fazie 7. "
                "MCP jest teraz jedyna sciezka dispatchu narzedzi. "
                "Dla testow uzyj InMemoryMcpTransport (patrz src/agent/mcp/README.md)."
            )

        if self._mcp_runtime is None:
            mcp_server = build_mcp_server(
                registry=self.tool_registry,
                ctx_provider=self._tool_ctx_provider,
                settings=self.mcp_settings,
            )
            self._mcp_runtime = McpRuntime(
                mcp=mcp_server,
                settings=self.mcp_settings,
                run_logger=self.run_logger,
            )

        try:
            await self._mcp_runtime.start()
        except Exception as exc:
            if self.run_logger is not None:
                self.run_logger.log_mcp_server_crashed(error=f"{type(exc).__name__}: {exc}")
            raise

        if self._mcp_client is None:
            self._mcp_client = McpAgentClient(
                settings=self.mcp_settings,
                run_logger=self.run_logger,
            )
        await self._mcp_client.ensure_connected()

    async def stop_mcp(self) -> None:
        """Zamyka klienta i zatrzymuje runtime. Idempotentne.

        Kolejnosc: najpierw klient (zeby nie zostawil otwartej sesji HTTP),
        potem serwer. Bledy zamkniecia sa logowane ale nie rzucane.
        """

        if self._mcp_client is not None:
            try:
                await self._mcp_client.close()
            except Exception as exc:
                logger.warning("MCP client close failed: %r", exc)
            self._mcp_client = None

        if self._mcp_runtime is not None:
            try:
                await self._mcp_runtime.stop()
            except Exception as exc:
                logger.warning("MCP runtime stop failed: %r", exc)
            self._mcp_runtime = None


class _ProposedPlanValidationError(Exception):
    """Wewnetrzny sygnal \u2014 AI zwrocilo cos niepoprawnego, nadaje sie do retry.

    Odroznia te bledy od bledow sieciowych / API (tamte sa RuntimeError).

    Pole ``executed_actions`` trzyma snapshot ``ctx.executed_actions`` z momentu,
    gdy walidacja padla — dzieki temu ``_propose_with_tool_loop`` moze przekazac
    "co juz zrobiles" do promptu kolejnej proby retry. Model nie zaczyna wtedy
    od eksploracji od zera, tylko wie, jakie write'y sa juz w buforze
    i moze od razu isc do ``submit_plan``.
    """

    def __init__(
        self,
        msg: str,
        *,
        executed_actions: "list[dict[str, Any]] | None" = None,
    ) -> None:
        super().__init__(msg)
        self.executed_actions: list[dict[str, Any]] = list(executed_actions or [])


def _register_default_tools(registry: ToolRegistry) -> None:
    """Rejestruje domyslne narzedzia (Fazy 2-3) w ``registry``.

    **Faza 2 - write na pelnym pliku:**

    - ``create_note`` / ``update_note`` / ``append_to_note`` - model
      produkuje caly nowy tekst notatki (wraz z frontmatterem).
    - ``submit_plan`` - terminator sesji.

    **Faza 3 - granulowany write:**

    - ``append_section`` / ``replace_section`` - operacje na sekcjach
      ``## heading``.
    - ``add_table_row`` - dopisanie wiersza do istniejacej tabeli GFM.
    - ``add_moc_link`` - idempotentne dopisanie bulletu ``- [[wikilink]]``
      do sekcji w MOC.
    - ``update_frontmatter`` - ustawienie pojedynczego pola YAML.
    - ``add_related_link`` - idempotentne dopisanie wikilinku do ``related[]``
      (bez zastapienia calej listy).

    **Faza 4 - eksploracja vaulta (read-only):**

    - ``list_notes`` - filtruj po type/parent/path_prefix + multi-tag
      (tags_any/tags_all/tags_none) + opcjonalny ``include_preview``.
    - ``read_note`` - tresc notatki + wikilinks_in/out; opcjonalnie wybrane sekcje.
    - ``find_related`` - fuzzy search po stem/title/tagach/headingach.
    - ``list_tags`` - mapa tagow z licznikami + top_paths per tag (Faza 5).
    - ``vault_map`` - drzewo MOC -> hub -> modul w jednym wywolaniu (Faza 5).
    - ``list_pending_concepts`` - orphan wikilinki (placeholdery do wypelnienia).
    - ``get_commit_context`` - metadane biezacego commita (SHA, pliki, stats).

    Wszystkie narzedzia write sa nieinwazyjne - rejestruja ``ProposedWrite``
    w ``ToolExecutionContext.proposed_writes``, finalizacja leci przez
    ``apply_pending`` -> preview -> user gating (``[T/n]``) w ``submit_plan``.

    **Faza 5 - domain creators (AthleteStack typology):**

    - ``create_hub`` - tworzy hub (wezel tematyczny) z parent_moc + sections[].
    - ``create_concept`` - tworzy notatke pojeciowa (definition/context/alternatives).
    - ``create_technology`` - tworzy notatke technologiczna (role/used_for/alternatives).
    - ``create_decision`` - tworzy ADR + **automatycznie** dopisuje wiersz do tabeli
      "Decyzje architektoniczne" w rodzicielskim hubie.
    - ``create_module`` - tworzy notatke modulu kodu (responsibility + elements).
    - ``create_changelog_entry`` - dodaje wpis commita do ``changelog/YYYY-MM-DD.md``
      (tworzy plik dnia gdy nie istnieje, inaczej dopisuje sekcje ``###``).
    - ``register_pending_concept`` - rejestruje orphan wikilink jako swiadomy
      placeholder w ``_Pending_Concepts.md`` (Faza 6).

    Duplikacje sa zgladzane przez ``ToolRegistry.register`` - jesli ktos
    wolal te funkcje dwa razy na tym samym registry, drugi call rzuci ValueError.
    """

    registry.register(CreateNoteTool())
    registry.register(UpdateNoteTool())
    registry.register(AppendToNoteTool())
    registry.register(AppendSectionTool())
    registry.register(ReplaceSectionTool())
    registry.register(AddTableRowTool())
    registry.register(AddMocLinkTool())
    registry.register(UpdateFrontmatterTool())
    registry.register(AddRelatedLinkTool())
    registry.register(ListNotesTool())
    registry.register(ReadNoteTool())
    registry.register(FindRelatedTool())
    registry.register(ListTagsTool())
    registry.register(VaultMapTool())
    registry.register(ListPendingConceptsTool())
    registry.register(RegisterPendingConceptTool())
    registry.register(GetCommitContextTool())
    registry.register(CreateHubTool())
    registry.register(CreateConceptTool())
    registry.register(CreateTechnologyTool())
    registry.register(CreateDecisionTool())
    registry.register(CreateModuleTool())
    registry.register(CreateChangelogEntryTool())
    registry.register(MocAuditTool())
    registry.register(MocSetIntroTool())
    registry.register(SubmitPlanTool())


ChunkProgressCallback = Callable[[int, int, DiffChunk, bool], None]
"""Callback wywolywany dla kazdego chunka w trybie multi-turn.

Sygnatura: ``(idx, total, chunk, cache_hit)``:

- ``idx``: 1-based numer chunka w calym commicie
- ``total``: ilosc wszystkich chunkow commita
- ``chunk``: sam ``DiffChunk`` (meta: file_path, chunk_idx, total_chunks)
- ``cache_hit``: True gdy summary przyszlo z cache; False gdy swieze
  wywolanie AI

Uzywany przez ``main.py`` do rich progress bar ("Chunk 3/7 | ai call...").
"""
