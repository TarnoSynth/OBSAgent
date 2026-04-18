"""Wykonuje ``ProposedWrite`` + plany MOC/index \u2014 best-effort, z raportem.

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

Kolejnosc aplikacji: najpierw wszystkie ``ProposedWrite`` w kolejnosci
w ktorej AI je zaproponowalo (to wazne \u2014 AI moze chciec ``create`` foo
a potem ``append`` do foo w tym samym batchu), potem wszystkie
``PlannedVaultWrite`` (MOC, index).
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from src.agent.models_actions import ProposedWrite
from src.agent.moc_planner import PlannedVaultWrite
from src.agent.pending import (
    PendingBatch,
    capture_snapshot,
    render_display_content,
    restore_from_snapshot,
)
from src.vault.manager import VaultManager


logger = logging.getLogger(__name__)


def _compute_append_separator(existing: str) -> str:
    """Replikuje separator dokladnie tak jak ``VaultManager.append``.

    Trzymamy lokalnie, zeby ``finalize_pending`` mogl zbudowac finalna
    czysta tresc bez zaleznosci od kolejnosci zapisow na dysku.
    """

    if existing.endswith("\n\n"):
        return ""
    if existing.endswith("\n"):
        return "\n"
    return "\n\n"


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
    """Aplikuje ``ProposedWrite`` i ``PlannedVaultWrite`` na vaulcie.

    Wszystkie zapisy ida przez ``VaultManager`` \u2014 executor nie siega do
    dysku bezposrednio. Dzieki temu walidacja bezpieczenstwa sciezki
    (np. ``..``, absolutne) zostaje jednoznacznie w warstwie vault.
    """

    def __init__(self, vault_manager: VaultManager) -> None:
        self.vault_manager = vault_manager

    def execute(
        self,
        actions: list[ProposedWrite],
        plans: list[PlannedVaultWrite],
    ) -> ActionExecutionReport:
        """Wykonuje akcje + plany w ustalonej kolejnosci. Nie rzuca wyjatkow.

        Sekwencja:

        1. Wszystkie ``ProposedWrite`` w kolejnosci pierwotnej.
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

    def _apply_action(self, action: ProposedWrite) -> ActionOutcome:
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

    def apply_pending(
        self,
        actions: list[ProposedWrite],
        plans: list[PlannedVaultWrite],
    ) -> tuple[ActionExecutionReport, PendingBatch]:
        """Zapisuje zmiany do vaulta w **diff-view** (red poprzednia + green nowa) + snapshot.

        Dla kazdej ``ProposedWrite`` plik jest zapisywany w trybie pending:

        - ``create`` → ``[frontmatter_new] + GREEN(body_new)``
        - ``update`` → ``[frontmatter_new] + RED(body_previous) + GREEN(body_new)``
        - ``append`` → ``previous_content + GREEN(delta_appended)``

        Sam render robi ``pending.render_display_content`` — tutaj tylko:

        1. Zbieramy **unikalny** zestaw sciezek (akcje + plany) i snapshot.
        2. Iterujemy po akcjach w kolejnosci oryginalnej, budujac per-path
           ``clean_content`` (po wszystkich akcjach) i flage ``had_wipe``
           (czy na sciezce byl create/update — poprzednia tresc zastapiona).
        3. Dla kazdej akcji **po jej zaaplikowaniu w pamieci** renderujemy
           display i zapisujemy na dysk. Kolejne akcje na tej samej sciezce
           re-renderuja display (z zywym stanem).
        4. Po akcjach aplikujemy plany MOC/indeksu (bez wrap-owania).
        5. Zwracamy raport + ``PendingBatch`` ze snapshotem i mapa
           clean-content — to dane do finalize (strip highlight) i rollback.

        **Best-effort:** wyjatek przy jednej akcji nie przerywa batcha.
        Padniete akcje trafiaja do raportu i do ``failed_action_paths``.
        Stan ``clean_by_path`` i ``had_wipe`` aktualizowany jest DOPIERO
        po udanym zapisie — zly write nie rozsypuje kolejnych iteracji.
        """

        report = ActionExecutionReport()
        touched: list[str] = []

        unique_paths: list[str] = []
        for path in [a.path for a in actions] + [p.path for p in plans]:
            if path not in unique_paths:
                unique_paths.append(path)

        snapshot = capture_snapshot(self.vault_manager, unique_paths)
        clean_by_path: dict[str, str] = {}
        wipe_by_path: dict[str, bool] = {}
        failed_action_paths: list[str] = []

        for action in actions:
            description = f"{action.type.upper()} {action.path}"
            previous_raw = snapshot.get(action.path)
            try:
                current_clean = clean_by_path.get(action.path)
                current_wipe = wipe_by_path.get(action.path, False)

                if action.type == "create":
                    if current_clean is not None or previous_raw is not None:
                        raise FileExistsError(
                            f"Create: plik {action.path!r} juz istnieje — uzyj 'overwrite' / 'append'."
                        )
                    new_clean = action.content
                    new_wipe = True
                elif action.type == "update":
                    if current_clean is None and previous_raw is None:
                        raise FileNotFoundError(
                            f"Update: plik {action.path!r} nie istnieje — uzyj 'create'."
                        )
                    new_clean = action.content
                    new_wipe = True
                elif action.type == "append":
                    base = current_clean if current_clean is not None else previous_raw
                    if base is None:
                        raise FileNotFoundError(
                            f"Append: plik {action.path!r} nie istnieje — uzyj 'create'."
                        )
                    sep = _compute_append_separator(base)
                    new_clean = base + sep + action.content
                    new_wipe = current_wipe
                else:
                    raise ValueError(f"Nieznany typ akcji: {action.type!r}")

                display = render_display_content(
                    clean_content=new_clean,
                    previous_raw=previous_raw,
                    had_wipe=new_wipe,
                )
                self.vault_manager.write_text(action.path, display)

                clean_by_path[action.path] = new_clean
                wipe_by_path[action.path] = new_wipe

                if action.path not in touched:
                    touched.append(action.path)

                logger.info("apply_pending: %s (diff-view)", description)
                report.outcomes.append(
                    ActionOutcome(
                        kind="agent_action",
                        path=action.path,
                        description=description,
                        success=True,
                    )
                )
            except Exception as exc:
                logger.exception("apply_pending: blad akcji %s: %s", description, exc)
                if action.path not in failed_action_paths:
                    failed_action_paths.append(action.path)
                report.outcomes.append(
                    ActionOutcome(
                        kind="agent_action",
                        path=action.path,
                        description=description,
                        success=False,
                        error_message=f"{type(exc).__name__}: {exc}",
                    )
                )

        plan_paths: list[str] = []
        for plan in plans:
            description = f"{plan.kind.upper()} {plan.path}"
            try:
                self.vault_manager.write_text(plan.path, plan.new_content)
                logger.info("apply_pending: %s (plan, no-highlight)", description)
                if plan.path not in plan_paths:
                    plan_paths.append(plan.path)
                if plan.path not in touched:
                    touched.append(plan.path)
                report.outcomes.append(
                    ActionOutcome(
                        kind=plan.kind,
                        path=plan.path,
                        description=description,
                        success=True,
                    )
                )
            except Exception as exc:
                logger.exception("apply_pending: blad planu %s: %s", description, exc)
                report.outcomes.append(
                    ActionOutcome(
                        kind=plan.kind,
                        path=plan.path,
                        description=description,
                        success=False,
                        error_message=f"{type(exc).__name__}: {exc}",
                    )
                )

        wipe_paths: list[str] = []
        create_paths: list[str] = []
        for path in clean_by_path.keys():
            had_wipe = wipe_by_path.get(path, False)
            if had_wipe and snapshot.get(path) is not None:
                wipe_paths.append(path)
            elif had_wipe and snapshot.get(path) is None:
                create_paths.append(path)

        report.touched_files = touched
        batch = PendingBatch(
            snapshot=snapshot,
            clean_by_path=clean_by_path,
            plan_paths=plan_paths,
            failed_action_paths=failed_action_paths,
            wipe_paths=wipe_paths,
            create_paths=create_paths,
        )
        return report, batch

    def finalize_pending(self, batch: PendingBatch) -> list[str]:
        """Usuwa zielone podswietlenie — nadpisuje pliki czysta trescia.

        Dziala tylko na ``clean_by_path`` (akcje AI). Plany MOC/indeksu
        sa pomijane — maja juz poprawna clean tresc na dysku.

        Zwraca liste faktycznie **przepisanych** sciezek. Nie commituje —
        to robi osobno ``Agent.commit_vault``.
        """

        rewritten: list[str] = []
        for rel_path, clean_content in batch.clean_by_path.items():
            if rel_path in batch.failed_action_paths:
                continue
            try:
                self.vault_manager.write_text(rel_path, clean_content)
                rewritten.append(rel_path)
                logger.info("finalize_pending: strip highlight w %s", rel_path)
            except Exception:
                logger.exception("finalize_pending: nie udalo sie sciagnac highlightu z %s", rel_path)
        return rewritten

    def rollback_pending(self, batch: PendingBatch) -> list[str]:
        """Przywraca vault ze snapshotu (akcje + plany).

        Zwraca liste faktycznie przywrocconych sciezek. Nie commituje
        niczego — rollback ma pozostawic vault dokladnie w stanie sprzed
        apply (z punktu widzenia Gita: zadnych niezacommitowanych
        zmian agentowych).
        """

        return restore_from_snapshot(self.vault_manager, batch.snapshot)

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
