"""``MOCAgent`` - drugi agent AI, dedykowany utrzymaniu MOC-u vaulta.

**Rola w architekturze:**

Glowny ``Agent`` (``src.agent.agent``) dokumentuje **kod projektu** -
per commit tworzy moduly, decyzje, changelog. Ale nie powinien odpowiadac
za **strukture nawigacyjna** MOC-u (huby, technologie, koncepty) - to
wymaga innego promptu i innego priorytetu: nie "diff z commita", tylko
"aktualny stan calego vaulta".

Dlatego MOCAgent jest **osobnym agentem** ktory:

- Nie patrzy na commity projektu. Bierze aktualny stan vaulta jako wejscie.
- Ma **dedykowany system prompt** (``Prompts/moc_system_<lang>.md``)
  instruujacy go w algorytmie: ``moc_audit`` -> plan -> akcje ->
  ``submit_plan``.
- Dzieli **provider AI, MCP client, ToolRegistry, VaultManager** z glownym
  agentem (kompozycja, nie dziedziczenie).
- Ma **wlasna petle tool-use** - prostsza (bez ``CommitInfo``, bez
  chunkow, bez retry z chunk summaries).
- Commituje osobno pod prefiksem ``Agent-MOC:``.

**Uruchamianie:**

1. **Flaga CLI** ``python main.py --moc-only`` - tylko MOCAgent, pomija
   dokumentowanie commitow.
2. **Delegacja z doc-agenta** - gdy doc-agent skonczy swoja petle lub
   nie ma nic do zrobienia, main.py woluje ``MOCAgent.run_session``
   automatycznie (konfigurowalne przez ``moc.delegate_after_docs``).

**Narzedzia:**

MOCAgent uzywa **tego samego** ``ToolRegistry`` co doc-agent (rejestracja
w ``_register_default_tools``). Nie filtrujemy toolsetu na poziomie
registry - LLM dostaje pelna liste i sam wybiera, co odpowiada jego
zadaniu (prompt wyraznie instruuje "nie twoz modulow, nie zmieniaj
modulow"). Przy wlaczaniu narzedzi specyficznych dla MOC
(``moc_audit``, ``moc_set_intro``) zyskalismy dwie dziwne z punktu
widzenia doc-agenta akcje, ktorych on nie ma powodu uzywac - ale kosztem
pojedynczych tokenow w jego prompt list (akceptowalne, bo alternatywa to
dwa osobne registry = skomplikowana re-inicjalizacja MCP per tryb).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from git import Actor, Repo
from git.exc import GitCommandError
from pydantic import ValidationError

from src.agent.models_actions import ProposedPlan, SessionResult
from src.agent.prompts import (
    load_moc_finalize_prompt,
    load_moc_system_prompt,
    load_moc_user_prompt,
)
from logs.context import LLMCallContext, llm_call_context
from src.agent.tools import ToolExecutionContext
from src.providers.base import ChatMessage, ChatRequest, MessageRole

if TYPE_CHECKING:
    from src.agent.action_executor import ActionExecutionReport, PendingBatch
    from src.agent.agent import Agent
    from src.vault.moc import BootstrapMocOutcome


logger = logging.getLogger(__name__)


#: Domyslne wartosci dla ``moc:`` sekcji w ``config.yaml``. Przenosimy je
#: wyzej niz AgentConfig zeby caller mogl wolac ``MOCAgent`` z jawnymi
#: defaultami bez ladowania calego yamla (przydatne w testach).
DEFAULT_MOC_ENABLED = True
DEFAULT_MOC_DELEGATE_AFTER_DOCS = False
DEFAULT_MOC_MAX_ITERATIONS = 20
DEFAULT_MOC_FORCE_SUBMIT_IN_LAST_N = 3
DEFAULT_MOC_BUDGET_HINT_LAST_N = 5
DEFAULT_MOC_COMMIT_PREFIX = "Agent-MOC: "


@dataclass
class MOCAgentConfig:
    """Konfiguracja MOCAgenta - dociagana z sekcji ``moc:`` w ``config.yaml``."""

    enabled: bool = DEFAULT_MOC_ENABLED
    delegate_after_docs: bool = DEFAULT_MOC_DELEGATE_AFTER_DOCS
    max_iterations: int = DEFAULT_MOC_MAX_ITERATIONS
    force_submit_in_last_n: int = DEFAULT_MOC_FORCE_SUBMIT_IN_LAST_N
    budget_hint_last_n: int = DEFAULT_MOC_BUDGET_HINT_LAST_N
    commit_prefix: str = DEFAULT_MOC_COMMIT_PREFIX
    moc_path: str = "MOC___Kompendium.md"


@dataclass
class MOCSessionResult:
    """Wynik pojedynczego biegu ``MOCAgent.run_session``.

    ``plan`` = None gdy MOCAgent wylaczony albo audyt nie znalazl nic do
    zrobienia i model sam zwrocil ``submit_plan`` bez rejestracji pisow.
    Caller (``main.py``) sprawdza ``plan is None`` albo ``plan.writes == []``
    i pomija preview/commit flow.
    """

    plan: ProposedPlan | None
    iterations_used: int
    tool_calls_count: int
    finalized_by_submit_plan: bool
    skipped_reason: str | None = None

    @property
    def has_changes(self) -> bool:
        """True gdy MOCAgent wygenerowal conajmniej jedna proponowana akcje."""

        return self.plan is not None and len(self.plan.writes) > 0


class MOCAgent:
    """Wyspecjalizowany agent AI do utrzymania MOC-u vaulta.

    Kompozycja nad ``Agent`` - dzieli provider, MCP client, VaultManager,
    ToolRegistry. Wlasna petla tool-use (prostsza, bez ``CommitInfo``).
    Commituje osobno pod prefiksem ``Agent-MOC:``.
    """

    def __init__(self, *, agent: "Agent", config: MOCAgentConfig) -> None:
        self._agent = agent
        self.config = config

    @classmethod
    def from_agent(cls, agent: "Agent") -> "MOCAgent":
        """Buduje MOCAgenta na podstawie aktywnego ``Agent`` + ``config.yaml``.

        Czyta sekcje ``moc:`` z tego samego pliku config co doc-agent,
        ladu defaults gdy brak. Blad konfiguracji rzuca ``ValueError``
        (spojnie z ``Agent.from_config``).
        """

        from src.agent.agent import load_config_dict

        cfg = load_config_dict(agent.config.config_path)
        moc_cfg = cfg.get("moc") or {}
        if not isinstance(moc_cfg, dict):
            raise ValueError("config: sekcja 'moc' musi byc mapa")

        def _bool(key: str, default: bool) -> bool:
            val = moc_cfg.get(key, default)
            return bool(val)

        def _int(key: str, default: int, *, minimum: int = 0) -> int:
            val = moc_cfg.get(key, default)
            try:
                n = int(val)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"config: moc.{key} musi byc liczba") from exc
            if n < minimum:
                raise ValueError(f"config: moc.{key} musi byc >= {minimum}")
            return n

        def _str(key: str, default: str) -> str:
            val = moc_cfg.get(key, default)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"config: moc.{key} musi byc niepustym stringiem")
            return val.strip() if val != default else val

        max_it = _int("max_iterations", DEFAULT_MOC_MAX_ITERATIONS, minimum=1)
        force_n = _int("force_submit_in_last_n", DEFAULT_MOC_FORCE_SUBMIT_IN_LAST_N)
        hint_n = _int("budget_hint_last_n", DEFAULT_MOC_BUDGET_HINT_LAST_N)
        if force_n > max_it:
            raise ValueError("config: moc.force_submit_in_last_n musi byc <= max_iterations")
        if hint_n > max_it:
            raise ValueError("config: moc.budget_hint_last_n musi byc <= max_iterations")

        moc_path = moc_cfg.get("moc_path")
        if moc_path is None:
            # bierzemy z bootstrap_moc_name jesli ustawiony, inaczej default
            from src.vault.moc import DEFAULT_MOC_PATTERN as _PAT
            bootstrap_name = agent.config.bootstrap_moc_name
            moc_path = _PAT.replace("{name}", bootstrap_name) + ".md"
        elif not isinstance(moc_path, str) or not moc_path.strip():
            raise ValueError("config: moc.moc_path musi byc niepustym stringiem")
        else:
            moc_path = moc_path.strip()

        config = MOCAgentConfig(
            enabled=_bool("enabled", DEFAULT_MOC_ENABLED),
            delegate_after_docs=_bool("delegate_after_docs", DEFAULT_MOC_DELEGATE_AFTER_DOCS),
            max_iterations=max_it,
            force_submit_in_last_n=force_n,
            budget_hint_last_n=hint_n,
            commit_prefix=_str("commit_prefix", DEFAULT_MOC_COMMIT_PREFIX),
            moc_path=moc_path,
        )
        return cls(agent=agent, config=config)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_session(
        self,
        *,
        trigger_context: str = "",
    ) -> MOCSessionResult:
        """Odpala jeden bieg MOCAgenta - audyt + akcje + submit_plan.

        Wymaga wczesniejszego ``agent.start_mcp()`` (tak samo jak
        ``Agent.run_session``). Petla tool-use pracuje w tym samym runtime
        MCP co doc-agent - roznia je tylko prompty i zakres semantyczny.

        :param trigger_context: tekstowe wyjasnienie skad MOCAgent zostal
            uruchomiony (np. ``"flaga --moc-only"``, ``"delegacja po
            zakonczeniu doc-agenta"``, ``"manualny rebuild po reset vaulta"``).
            Wklejane w user-prompt jako kontekst sytuacyjny.
        """

        if not self.config.enabled:
            return MOCSessionResult(
                plan=None,
                iterations_used=0,
                tool_calls_count=0,
                finalized_by_submit_plan=False,
                skipped_reason="moc_agent_disabled",
            )

        agent = self._agent

        if agent.mcp_client is None or not agent.mcp_client.connected:
            raise RuntimeError(
                "MOCAgent.run_session wymaga aktywnego MCP - wywolaj agent.start_mcp() przed sesja."
            )

        language = agent.config.language
        system_prompt = load_moc_system_prompt(language).replace(
            "{{max_tool_iterations}}", str(self.config.max_iterations)
        )
        finalize_prompt = load_moc_finalize_prompt(language)

        user_prompt_raw = load_moc_user_prompt(language)
        user_prompt = (
            user_prompt_raw
            .replace("{{project_name}}", agent.config.project_name)
            .replace("{{vault_path}}", str(agent.config.vault_path))
            .replace("{{moc_path}}", self.config.moc_path)
            .replace("{{trigger_context}}", trigger_context or "(brak dodatkowego kontekstu)")
        )

        ctx = ToolExecutionContext(
            vault_manager=agent.vault_manager,
            run_logger=agent.run_logger,
        )
        agent.set_tool_ctx(ctx)

        try:
            return await self._run_tool_loop(
                ctx=ctx,
                system_prompt=system_prompt,
                finalize_prompt=finalize_prompt,
                user_prompt=user_prompt,
            )
        finally:
            agent.clear_tool_ctx()

    def apply_pending(
        self,
        plan: ProposedPlan,
    ) -> "tuple[ActionExecutionReport, PendingBatch]":
        """Cienka fasada na ``Agent.apply_pending`` - bez post-plans dla MOC.

        MOC nie potrzebuje ``plan_post_updates`` - nie tworzy modulow ktore
        trzeba dopinac do MOC-a (MOC tez nie trzeba dopinac do _index.md,
        bo _index zarzadza ``MOCManager`` osobnym torem).
        """

        return self._agent.apply_pending(plan, [])

    def finalize_pending(self, batch: "PendingBatch") -> list[str]:
        """Fasada na ``Agent.finalize_pending`` - uzgadnia stan po approve."""

        return self._agent.finalize_pending(batch)

    def rollback_pending(self, batch: "PendingBatch") -> list[str]:
        """Fasada na ``Agent.rollback_pending`` - restore ze snapshotu po reject."""

        return self._agent.rollback_pending(batch)

    def commit_vault_moc(
        self,
        *,
        approved: bool,
        execution_report: "ActionExecutionReport",
        summary: str,
    ) -> str:
        """Commituje zmiany MOC pod prefiksem ``Agent-MOC:``.

        Analogia do ``Agent.commit_vault`` ale bez ``project_commit`` -
        MOC nie jest zwiazany z zadnym commitem projektu. Subject ma
        format ``Agent-MOC: <N> zmian`` (liczba touched_files) albo
        ``Agent-MOC: <nazwa MOC>`` gdy modifikowany byl tylko jeden plik.
        """

        if not approved:
            raise RuntimeError(
                "commit_vault_moc: approved=False - odmowa zapisu. MOCAgent "
                "nie commituje bez zgody usera."
            )

        touched = execution_report.touched_files
        if not touched:
            raise RuntimeError(
                "commit_vault_moc: brak plikow do zacommitowania - wszystkie akcje padly."
            )

        agent = self._agent
        repo = Repo(agent.config.vault_path)
        abs_paths = [str((agent.config.vault_path / p).resolve()) for p in touched]

        try:
            repo.index.add(abs_paths)
        except GitCommandError as exc:
            raise RuntimeError(f"git add zwrocil blad w vaulcie (MOC): {exc}") from exc

        if len(touched) == 1:
            subject = f"{self.config.commit_prefix}rebuild {touched[0]}"
        else:
            subject = f"{self.config.commit_prefix}rebuild ({len(touched)} plikow)"

        files_list = "\n".join(f"- {p}" for p in touched)
        body = f"{summary}\n\nZmienione pliki:\n{files_list}"
        message = f"{subject}\n\n{body}"

        author = Actor("obsidian-doc-agent", "agent@local")
        try:
            commit = repo.index.commit(message, author=author, committer=author)
        except Exception as exc:
            raise RuntimeError(f"git commit MOC na vaulcie sie nie udal: {exc}") from exc

        logger.info("Zacommitowano MOC vault: %s - %s", commit.hexsha[:7], subject)
        return commit.hexsha

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _run_tool_loop(
        self,
        *,
        ctx: ToolExecutionContext,
        system_prompt: str,
        finalize_prompt: str,
        user_prompt: str,
    ) -> MOCSessionResult:
        """Uproszczona petla tool-use dla MOCAgenta - bez chunkow i ``CommitInfo``.

        Odpowiednik ``Agent._run_tool_loop`` ale:

        - phase stalo ``"moc"``
        - brak retry z chunk summaries (MOC nie ma chunkow)
        - LLMCallContext ma puste ``files`` i syntetyczny sha ``"moc-rebuild"``
        - blad "model nie wolal narzedzia" / "max_iterations bez submit_plan"
          idzie jako ``ValueError`` - main.py decyduje co z tym (zwykle log
          + pomin commit)
        """

        agent = self._agent
        tool_definitions = await agent.mcp_client.list_tools()
        if not tool_definitions:
            raise RuntimeError(
                "MCP nie wystawia zadnych narzedzi w MOCAgent - sprawdz _register_default_tools."
            )

        messages: list[ChatMessage] = [
            ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
            ChatMessage(role=MessageRole.SYSTEM, content=finalize_prompt),
            ChatMessage(role=MessageRole.USER, content=user_prompt),
        ]

        tool_calls_total = 0
        iterations_used = 0
        max_iter = self.config.max_iterations
        force_last_n = self.config.force_submit_in_last_n
        hint_last_n = self.config.budget_hint_last_n

        for iteration in range(1, max_iter + 1):
            iterations_used = iteration
            iterations_left = max_iter - iteration

            tool_choice_value: str | dict[str, Any] = "auto"
            if force_last_n > 0 and iterations_left < force_last_n:
                tool_choice_value = {"type": "tool", "name": "submit_plan"}

            call_ctx = LLMCallContext(
                phase="moc",
                commit_sha="moc-rebuild",
                attempt=1,
                files=tuple(),
                iteration=iteration,
            )
            request = ChatRequest(
                messages=messages,
                tools=tool_definitions,
                tool_choice=tool_choice_value,
                parallel_tool_calls=True,
            )

            with llm_call_context(call_ctx):
                result = await agent.provider.complete(request)

            messages.append(
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content=result.text or None,
                    tool_calls=list(result.tool_calls),
                )
            )

            if not result.tool_calls:
                if iteration == 1:
                    raise RuntimeError(
                        "MOCAgent: model nie wywolal zadnego narzedzia w pierwszej turze. "
                        "Minimum to moc_audit + submit_plan."
                    )
                break

            budget_hint_suffix = ""
            if hint_last_n > 0 and iterations_left < hint_last_n:
                budget_hint_suffix = self._format_budget_hint(
                    iteration=iteration,
                    max_iter=max_iter,
                    iterations_left=iterations_left,
                    force_last_n=force_last_n,
                )

            tool_calls_total += len(result.tool_calls)
            dispatch_coros = [
                agent._dispatch_via_mcp(tool_call) for tool_call in result.tool_calls
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
            raise RuntimeError(
                f"MOCAgent: przekroczono max_iterations={max_iter} bez submit_plan."
            )

        if not ctx.finalized:
            raise RuntimeError(
                "MOCAgent: model przestal wolac narzedzia bez submit_plan."
            )

        plan = self._build_plan(ctx)

        return MOCSessionResult(
            plan=plan,
            iterations_used=iterations_used,
            tool_calls_count=tool_calls_total,
            finalized_by_submit_plan=True,
        )

    def _build_plan(self, ctx: ToolExecutionContext) -> ProposedPlan:
        """Buduje ``ProposedPlan`` z ``ctx.proposed_writes`` + summary.

        Analogia do ``Agent._build_proposed_plan`` ale zwraca ProposedPlan
        albo rzuca ``ValueError`` (MOCAgent nie potrzebuje retry logiki
        z chunk summaries - jeden bad run po prostu loguje i kończy).
        """

        summary = (ctx.final_summary or "").strip()
        if not summary:
            raise ValueError("MOCAgent: submit_plan nie dostarczyl niepustego summary.")
        try:
            return ProposedPlan(summary=summary, writes=list(ctx.proposed_writes))
        except ValidationError as exc:
            raise ValueError(f"MOCAgent: budowa ProposedPlan padla: {exc}") from exc

    @staticmethod
    def _format_budget_hint(
        *,
        iteration: int,
        max_iter: int,
        iterations_left: int,
        force_last_n: int,
    ) -> str:
        """Sufix doklejany do tool_result w koncowych iteracjach."""

        lines = [
            "",
            "",
            f"[budzet-moc: iteracja {iteration}/{max_iter}, pozostalo {iterations_left}]",
        ]
        if force_last_n > 0 and iterations_left < force_last_n:
            lines.append(
                "[TWARDE WYMUSZENIE: kolejna iteracja ma tool_choice=submit_plan. "
                "Zakoncz TERAZ.]"
            )
        elif iterations_left <= 2:
            lines.append(
                "[UWAGA: koniec petli - jesli masz co podsumowac, wywolaj submit_plan.]"
            )
        return "\n".join(lines)


__all__ = [
    "DEFAULT_MOC_COMMIT_PREFIX",
    "DEFAULT_MOC_DELEGATE_AFTER_DOCS",
    "DEFAULT_MOC_ENABLED",
    "DEFAULT_MOC_MAX_ITERATIONS",
    "MOCAgent",
    "MOCAgentConfig",
    "MOCSessionResult",
]
