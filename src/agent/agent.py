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

Komunikacja z AI idzie **wylacznie** przez tool calling: agent rejestruje
narzedzie ``submit_plan`` ze schematem ``AgentResponse`` i forsuje
``tool_choice=required``. Retry 2x z bledem w prompcie (decyzja Q5).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from git import Actor, GitCommandError, Repo
from pydantic import ValidationError

if TYPE_CHECKING:
    from logs.run_logger import RunLogger

from src.agent.action_executor import ActionExecutionReport, ActionExecutor
from src.agent.chunk_cache import ChunkCache
from src.agent.git_context import GitContextBuilder
from src.agent.pending import PendingBatch
from src.agent.models import AgentState, VaultSnapshot
from src.agent.models_actions import (
    SUBMIT_PLAN_TOOL_DESCRIPTION,
    SUBMIT_PLAN_TOOL_NAME,
    AgentResponse,
    build_submit_plan_schema,
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
from src.agent.templates import load_all_templates
from src.git.models import CommitInfo
from src.git.reader import GitReader
from src.git.syncer import GitSyncer
from src.providers import (
    BaseProvider,
    ChatMessage,
    ChatRequest,
    MessageRole,
    ProviderResult,
    ToolDefinition,
    ToolFunctionDefinition,
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
        )

        provider = build_provider(resolved, run_logger=run_logger)
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
    ) -> AgentResponse:
        """Wola AI z retry i zwraca zwalidowany ``AgentResponse``.

        Dwie sciezki (decyzja user: `delivery=summarize_first`):

        - **Small** (``chunked_commit.is_small()``): wszystkie chunki
          miesza sie w jednym requescie. System prompt + pelny
          ``build_user_prompt`` + ``tool_choice=required`` \u2192 AI od
          razu wola ``submit_plan``.

        - **Chunked**: iterujemy po chunkach, dla kazdego zapytanie AI
          o 3-6 zdaniowe podsumowanie (cache-owane per ``(sha, path,
          chunk_idx)`` w ``ChunkCache``). Potem FINALIZE: system prompt
          + ``build_finalize_prompt`` z zebranymi podsumowaniami +
          ``tool_choice=required``.

        Retry walidacji odpowiedzi AI obejmuje WYLACZNIE finalny step
        (submit_plan). Chunk-summaries sa zwyklym tekstem \u2014 nie ma
        schematu Pydantic do zawalenia, wiec nie robimy tam retry.

        :param on_chunk_progress: opcjonalny callback ``(idx, total, chunk,
            cache_hit)`` wolany dla kazdego chunka \u2014 do UI progress.
        """

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
    ) -> AgentResponse:
        """Jeden request \u2014 caly kontekst + submit_plan od razu."""

        system_prompt = self._system_prompt()
        retry_error: str | None = None
        last_exc: Exception | None = None

        for attempt in range(self.config.max_retries + 1):
            user_prompt = build_user_prompt(
                chunked_commit=chunked_commit,
                vault_changes=vault_changes,
                vault_changed_notes=vault_changed_notes,
                vault_knowledge=vault_knowledge,
                templates=templates,
                project_name=self.config.project_name,
                retry_error=retry_error,
            )

            request = ChatRequest(
                messages=[
                    ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
                    ChatMessage(role=MessageRole.USER, content=user_prompt),
                ],
                tools=[self._submit_plan_tool()],
                tool_choice="required",
                parallel_tool_calls=False,
            )

            call_ctx = LLMCallContext(
                phase="SMALL",
                commit_sha=chunked_commit.commit.sha,
                attempt=attempt + 1,
                files=tuple(c.path for c in chunked_commit.commit.changes),
            )
            try:
                with llm_call_context(call_ctx):
                    result = await self.provider.complete(request)
                return self._parse_agent_response(result)
            except _AgentResponseValidationError as exc:
                last_exc = exc
                retry_error = str(exc)
                logger.warning(
                    "SMALL attempt %d/%d: blad walidacji odpowiedzi AI: %s",
                    attempt + 1, self.config.max_retries + 1, exc,
                )
                continue
            except Exception as exc:
                raise RuntimeError(f"Blad wywolania providera {self.provider.name}: {exc}") from exc

        raise RuntimeError(
            f"AI nie zwrocilo poprawnej odpowiedzi po {self.config.max_retries + 1} probach. "
            f"Ostatni blad: {last_exc}"
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
    ) -> AgentResponse:
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
    ) -> AgentResponse:
        """FINALIZE: zebrane podsumowania \u2192 submit_plan. Retry jak w small."""

        system_prompt = self._system_prompt()
        finalize_extra_prompt = self._finalize_prompt()
        retry_error: str | None = None
        last_exc: Exception | None = None

        for attempt in range(self.config.max_retries + 1):
            user_prompt = build_finalize_prompt(
                chunked_commit=chunked_commit,
                chunk_summaries=chunk_summaries,
                vault_changes=vault_changes,
                vault_changed_notes=vault_changed_notes,
                vault_knowledge=vault_knowledge,
                templates=templates,
                project_name=self.config.project_name,
                retry_error=retry_error,
            )

            request = ChatRequest(
                messages=[
                    ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
                    ChatMessage(role=MessageRole.SYSTEM, content=finalize_extra_prompt),
                    ChatMessage(role=MessageRole.USER, content=user_prompt),
                ],
                tools=[self._submit_plan_tool()],
                tool_choice="required",
                parallel_tool_calls=False,
            )

            call_ctx = LLMCallContext(
                phase="FINALIZE",
                commit_sha=chunked_commit.commit.sha,
                chunk_total=chunked_commit.total_chunks,
                attempt=attempt + 1,
                files=tuple(c.path for c in chunked_commit.commit.changes),
            )
            try:
                with llm_call_context(call_ctx):
                    result = await self.provider.complete(request)
                return self._parse_agent_response(result)
            except _AgentResponseValidationError as exc:
                last_exc = exc
                retry_error = str(exc)
                logger.warning(
                    "FINALIZE attempt %d/%d: blad walidacji odpowiedzi AI: %s",
                    attempt + 1, self.config.max_retries + 1, exc,
                )
                continue
            except Exception as exc:
                raise RuntimeError(
                    f"Blad wywolania providera {self.provider.name} przy FINALIZE: {exc}"
                ) from exc

        raise RuntimeError(
            f"FINALIZE: AI nie zwrocilo poprawnej odpowiedzi po "
            f"{self.config.max_retries + 1} probach. Ostatni blad: {last_exc}"
        )

    def plan_post_updates(
        self,
        response: AgentResponse,
        knowledge: VaultKnowledge,
    ) -> list[PlannedVaultWrite]:
        """Pre-compute plany MOC i indeksu dla zaproponowanych akcji."""

        if not response.actions:
            return []
        return plan_post_action_updates(
            response.actions,
            self.vault_manager,
            knowledge,
            index_path=self.config.vault_index_filename,
        )

    def execute_plan(
        self,
        response: AgentResponse,
        plans: list[PlannedVaultWrite],
    ) -> ActionExecutionReport:
        """Aplikuje akcje + plany na vaulcie. Best-effort, zwraca raport.

        **Legacy** — zapisuje bez podswietlenia i bez snapshotu.
        Nowy flow uzywa ``apply_pending`` + ``finalize_pending`` /
        ``rollback_pending``. Zostawione dla testow/dry-run.
        """

        return self.action_executor.execute(response.actions, plans)

    def apply_pending(
        self,
        response: AgentResponse,
        plans: list[PlannedVaultWrite],
    ) -> tuple[ActionExecutionReport, PendingBatch]:
        """Zapisuje zmiany do vaulta Z ZIELONYM PODSWIETLENIEM + snapshot.

        Cienka fasada wokol ``ActionExecutor.apply_pending``. User musi
        potem przejrzec vault w Obsidianie i wybrac:

        - ``finalize_pending`` (akceptacja — usuwa zielone tlo + commit)
        - ``rollback_pending`` (odrzucenie — restore ze snapshotu, bez commita)
        """

        return self.action_executor.apply_pending(response.actions, plans)

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
            self._system_prompt_cache = load_system_prompt(self.config.language)
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

    def _submit_plan_tool(self) -> ToolDefinition:
        return ToolDefinition(
            function=ToolFunctionDefinition(
                name=SUBMIT_PLAN_TOOL_NAME,
                description=SUBMIT_PLAN_TOOL_DESCRIPTION,
                parameters=build_submit_plan_schema(),
            )
        )

    @staticmethod
    def _parse_agent_response(result: ProviderResult) -> AgentResponse:
        """Wyciaga tool_call ``submit_plan`` i parsuje argumenty przez Pydantic."""

        submit_call = next(
            (tc for tc in result.tool_calls if tc.function.name == SUBMIT_PLAN_TOOL_NAME),
            None,
        )
        if submit_call is None:
            raise _AgentResponseValidationError(
                "Model nie wywolal narzedzia `submit_plan`. Wywolaj je DOKLADNIE RAZ "
                "z argumentami zgodnymi ze schematem."
            )

        raw_args = submit_call.function.arguments
        try:
            data = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError as exc:
            raise _AgentResponseValidationError(
                f"Argumenty tool_call `submit_plan` nie sa poprawnym JSON-em: {exc.msg} (linia {exc.lineno})."
            ) from exc

        try:
            return AgentResponse.model_validate(data)
        except ValidationError as exc:
            raise _AgentResponseValidationError(
                f"Walidacja Pydantic AgentResponse nie przeszla: {exc}"
            ) from exc


class _AgentResponseValidationError(Exception):
    """Wewnetrzny sygnal \u2014 AI zwrocilo cos niepoprawnego, nadaje sie do retry.

    Odroznia te bledy od bledow sieciowych / API (tamte sa RuntimeError).
    """

    pass


from typing import Callable  # noqa: E402  (forward type na koncu, zeby nie komplikowac importow wyzej)


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
