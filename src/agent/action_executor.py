"""Wykonuje ``AgentAction`` + plany MOC/index \u2014 best-effort, z raportem.

Executor jest **glupi**: bierze liste akcji (po walidacji Pydantic
i po akceptacji ``[T]`` uzytkownika) i sekwencyjnie je aplikuje przez
``VaultManager``. Nie podejmuje decyzji semantycznych.

Polityka bledu (roadmap Faza 6):

- **Best-effort** \u2014 wyjatek w trakcie jednej akcji **NIE** przerywa
  batcha. Blad trafia do raportu (``ActionExecutionReport``), petla
  leci dalej.
- Na koncu executor zwraca raport z lista ``succeeded`` i ``failed``
  plus lista plikow ktore realnie trafily na dysk (``touched_files``).
  Commit na vaulta otrzymuje ten sam raport i commituje tylko to, co
  sie udalo zapisac.

Rollback nie jest robiony w locie \u2014 jesli user chce cofnac, robi to
rekami (``git revert`` lub rewert pojedynczych plikow). To zgodne z
polityka "commit gated by approval, but best-effort po commicie" \u2014
po akceptacji user i tak dostaje jedno okno do rewertu.

Kolejnosc aplikacji: najpierw wszystkie ``AgentAction`` w kolejnosci
w ktorej AI je zaproponowalo (to wazne \u2014 AI moze chciec ``create`` foo
a potem ``append`` do foo w tym samym batchu), potem wszystkie
``PlannedVaultWrite`` (MOC, index).
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from src.agent.models_actions import AgentAction
from src.agent.moc_planner import PlannedVaultWrite
from src.vault.manager import VaultManager


logger = logging.getLogger(__name__)


class ActionOutcome(BaseModel):
    """Wynik pojedynczej operacji w batchu \u2014 akcji AI albo planu MOC/index."""

    kind: str
    """``agent_action`` | ``moc_append`` | ``index_update`` | ``index_create``"""

    path: str
    description: str = Field(..., description="Human-readable opis co mialo sie stac")
    success: bool
    error_message: str | None = None


class ActionExecutionReport(BaseModel):
    """Raport z wykonania calego batcha \u2014 dane do commita i log\u00f3w.

    ``touched_files`` zawiera **tylko** sciezki plikow, ktore realnie
    trafily na dysk (operacje ``success=True``). Commit uzywa tej listy
    do ``git add <path>`` zamiast ``git add -A`` (chroni przed
    commitowaniem niespokrewnionych zmian uzytkownika).
    """

    outcomes: list[ActionOutcome] = Field(default_factory=list)
    touched_files: list[str] = Field(default_factory=list)

    @property
    def succeeded(self) -> list[ActionOutcome]:
        return [o for o in self.outcomes if o.success]

    @property
    def failed(self) -> list[ActionOutcome]:
        return [o for o in self.outcomes if not o.success]

    @property
    def has_failures(self) -> bool:
        return any(not o.success for o in self.outcomes)


class ActionExecutor:
    """Aplikuje ``AgentAction`` i ``PlannedVaultWrite`` na vaulcie.

    Wszystkie zapisy ida przez ``VaultManager`` \u2014 executor nie siega do
    dysku bezposrednio. Dzieki temu walidacja bezpieczenstwa sciezki
    (np. ``..``, absolutne) zostaje jednoznacznie w warstwie vault.
    """

    def __init__(self, vault_manager: VaultManager) -> None:
        self.vault_manager = vault_manager

    def execute(
        self,
        actions: list[AgentAction],
        plans: list[PlannedVaultWrite],
    ) -> ActionExecutionReport:
        """Wykonuje akcje + plany w ustalonej kolejnosci. Nie rzuca wyjatkow.

        Sekwencja:

        1. Wszystkie ``AgentAction`` w kolejnosci pierwotnej.
        2. Wszystkie ``PlannedVaultWrite`` w kolejnosci pierwotnej
           (moc_planner.plan_post_action_updates sortuje juz po sciezce).

        Zwraca ``ActionExecutionReport`` \u2014 caly batch, zarowno sukcesy
        jak i bledy. Wolajacy decyduje co robic dalej (zwykle: commit
        ``touched_files``, wyswietl bledy, zapisz logi).
        """

        report = ActionExecutionReport()
        touched: list[str] = []

        for action in actions:
            outcome = self._apply_action(action)
            report.outcomes.append(outcome)
            if outcome.success and action.path not in touched:
                touched.append(action.path)

        for plan in plans:
            outcome = self._apply_plan(plan)
            report.outcomes.append(outcome)
            if outcome.success and plan.path not in touched:
                touched.append(plan.path)

        report.touched_files = touched
        return report

    def _apply_action(self, action: AgentAction) -> ActionOutcome:
        description = f"{action.type.upper()} {action.path}"
        try:
            if action.type == "create":
                self.vault_manager.create(action.path, action.content)
            elif action.type == "update":
                self.vault_manager.overwrite(action.path, action.content)
            elif action.type == "append":
                self.vault_manager.append(action.path, action.content)
            else:
                raise ValueError(f"Nieznany typ akcji: {action.type!r}")

            logger.info("Wykonano akcje: %s", description)
            return ActionOutcome(
                kind="agent_action",
                path=action.path,
                description=description,
                success=True,
            )
        except Exception as exc:
            logger.exception("Blad akcji %s: %s", description, exc)
            return ActionOutcome(
                kind="agent_action",
                path=action.path,
                description=description,
                success=False,
                error_message=f"{type(exc).__name__}: {exc}",
            )

    def _apply_plan(self, plan: PlannedVaultWrite) -> ActionOutcome:
        description = f"{plan.kind.upper()} {plan.path}"
        try:
            self.vault_manager.write_text(plan.path, plan.new_content)
            logger.info("Wykonano plan: %s", description)
            return ActionOutcome(
                kind=plan.kind,
                path=plan.path,
                description=description,
                success=True,
            )
        except Exception as exc:
            logger.exception("Blad planu %s: %s", description, exc)
            return ActionOutcome(
                kind=plan.kind,
                path=plan.path,
                description=description,
                success=False,
                error_message=f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    def rollback_touched_files(
        vault_manager: VaultManager,
        touched_files: list[str],
        *,
        snapshots: dict[str, str | None],
    ) -> None:
        """Cofa zmiany na plikach na podstawie snapshotu ``path -> previous_content``.

        Uzywane gdy user odmowil ``[T/n]`` **po** wykonaniu akcji, a przed
        commitem \u2014 w naszym flow to NIE wystepuje (decyzja jest PRZED
        wykonaniem), ale zostawiamy jako utility na future use (retry
        po odmowie).

        ``snapshots[path]=None`` oznacza, ze plik nie istnial \u2014 zostanie
        usuniety. ``snapshots[path]="..."`` przywraca poprzednia tresc.
        """

        for path in touched_files:
            if path not in snapshots:
                logger.warning("Rollback: brak snapshotu dla %s \u2014 pomijam", path)
                continue
            prev = snapshots[path]
            try:
                if prev is None:
                    if vault_manager.note_exists(path):
                        vault_manager.delete(path)
                else:
                    vault_manager.write_text(path, prev)
            except Exception:
                logger.exception("Rollback: blad przy przywracaniu %s", Path(path).as_posix())
