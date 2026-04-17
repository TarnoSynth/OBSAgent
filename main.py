"""Obsidian Git Documentation Agent \u2014 punkt wejscia z petla iteracyjna.

**Flow:** dla kazdego nieprzetworzonego commita projektu, od najstarszego:

1. Pull obu repo (``Agent.sync_repos``)
2. Skan vaulta i zbiorka recznych zmian usera (``Agent.scan_vault`` + ``collect_vault_changes``)
3. Chunkuj diff commita (``Agent.prepare_commit_for_ai`` \u2192 ``ChunkedCommit``)
4. Wywolanie AI z tool callingiem (``Agent.propose_actions``):
   - Small commit: jeden request + ``submit_plan``
   - Duzy commit: multi-turn chunk-summary (cache po sha+path+idx) \u2192 FINALIZE + ``submit_plan``
5. Pre-compute planow MOC/indeksu (``Agent.plan_post_updates``)
6. Preview w terminalu (``PreviewRenderer.render_plan``)
7. Pytanie usera:
   - ``[T]`` \u2192 aplikuj akcje + plany, zacommituj vault, dopisz do state
   - ``[n]`` \u2192 stop; pytanie o retry tego samego commita; jesli nie \u2014 koniec biegu

**Zasada bezpieczenstwa:** ``Agent.commit_vault`` jest **jedynym miejscem**
gdzie ten proces robi commit na repo vaulta. Nigdy nie woluje ``push``.

Konfiguracja przez ``config.yaml`` + ``.env`` (klucze API). Petla nie
rusza sie z miejsca bez zatwierdzenia usera \u2014 to celowa decyzja
architektoniczna (zobacz ROADMAP_AGENT.md, Faza 6).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from src.agent import (
    Agent,
    AgentResponse,
    PendingBatch,
    PreviewRenderer,
    ask_accept_pending,
    ask_retry,
)
from src.agent.action_executor import ActionExecutionReport
from src.agent.models_chunks import ChunkedCommit, DiffChunk
from src.agent.moc_planner import PlannedVaultWrite
from src.git.models import CommitInfo

logger = logging.getLogger(__name__)
console = Console()


def _configure_logging() -> None:
    """Loguje na stderr na poziomie WARNING \u2014 szczegoly i tak lecą do rich UI."""

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _render_pending_review_banner(batch: PendingBatch, *, vault_path: Path) -> None:
    """Banner z instrukcja: idz do Obsidiana i przejrzyj diff-view (red + green)."""

    wipe_paths = set(batch.wipe_paths)
    create_paths = set(batch.create_paths)

    lines: list[str] = []
    lines.append(f"[bold]Vault:[/] [white]{vault_path}[/]")
    lines.append("")

    new_files = [p for p in batch.clean_by_path.keys() if p in create_paths]
    updated_files = [p for p in batch.clean_by_path.keys() if p in wipe_paths]
    append_files = [
        p for p in batch.clean_by_path.keys()
        if p not in create_paths and p not in wipe_paths
    ]

    if new_files:
        lines.append("[bold]NOWE notatki[/] [green](tylko zielony blok \u2014 create)[/]:")
        for p in new_files:
            lines.append(f"  [green]\u25c9 GREEN[/] {p}")
        lines.append("")

    if updated_files:
        lines.append(
            "[bold]ZAKTUALIZOWANE notatki[/] [red](czerwony = poprzednie)[/] + "
            "[green](zielony = nowe)[/]:"
        )
        for p in updated_files:
            lines.append(f"  [red]\u25c9 RED[/] + [green]\u25c9 GREEN[/] {p}")
        lines.append("")

    if append_files:
        lines.append("[bold]DOPISANE fragmenty[/] [green](zielony dopisek na koncu)[/]:")
        for p in append_files:
            lines.append(f"  [dim]\u25cb original[/] + [green]\u25c9 GREEN[/] {p}")
        lines.append("")

    if not batch.clean_by_path:
        lines.append("[dim](brak akcji AI \u2014 wszystkie padly)[/]")
        lines.append("")

    if batch.plan_paths:
        lines.append("[bold]Zaktualizowany MOC / _index.md[/] [dim](bez podswietlenia)[/]:")
        for p in batch.plan_paths:
            lines.append(f"  [magenta]\u25cb[/] {p}")
        lines.append("")

    lines.append(
        "[yellow]Otworz vault w Obsidianie \u2014 czerwone bloki pokazuja TO, CO BYLO, "
        "zielone TO, CO AGENT PROPONUJE.[/]"
    )
    lines.append(
        "[yellow]NIE commituj recznie[/] \u2014 agent commituje po Twojej akceptacji."
    )
    lines.append("")
    lines.append(
        "[green]T[/] \u2192 czerwone bloki znikaja, zostaje zielona tresc jako czysty plik, "
        "agent commituje vault."
    )
    lines.append(
        "[red]n[/] \u2192 vault cofniety do stanu sprzed propozycji (czerwone wraca jako zywa tresc)."
    )

    console.print(
        Panel(
            "\n".join(lines),
            title="[bold]Czekam na decyzje \u2014 przejrzyj diff-view w Obsidianie[/]",
            border_style="green",
        )
    )


async def _process_single_commit(
    agent: Agent,
    state,
    project_commit: CommitInfo,
    renderer: PreviewRenderer,
) -> bool:
    """Przetwarza **jeden** commit projektu end-to-end.

    Zwraca:

    - ``True``  \u2014 commit zostal przetworzony (zaakceptowany lub pominiety,
      w obu przypadkach dopisany do state); petla moze iterowac dalej.
    - ``False`` \u2014 user odrzucil i nie chce retry; petla konczy sie teraz.

    Retry jest **wewnetrzny** \u2014 przy odrzuceniu tego commita pytamy o
    ponowne wygenerowanie planu dla **tego samego** commita. Nie dotyczy
    retry walidacji AI (ten siedzi w ``Agent.propose_actions``).
    """

    while True:
        console.rule(f"[bold cyan]Commit projektu: {project_commit.sha[:7]}")
        subject = project_commit.message.splitlines()[0][:120] if project_commit.message else "(bez wiadomosci)"
        console.print(f"[dim]{project_commit.date.isoformat()} \u2022 {project_commit.author}[/]")
        console.print(f"[white]{subject}[/]\n")

        knowledge = agent.scan_vault()
        vault_changes, vault_changed_notes = agent.collect_vault_changes(state)
        chunked_commit: ChunkedCommit = agent.prepare_commit_for_ai(project_commit)

        if chunked_commit.is_small():
            console.print(
                f"[cyan]Commit maly ({chunked_commit.total_chunks} chunk) \u2014 jeden request do AI\u2026[/]"
            )
        else:
            console.print(
                f"[cyan]Commit duzy \u2014 {chunked_commit.total_chunks} chunkow, tryb multi-turn "
                f"(chunk-summary + FINALIZE, cache w .agent-cache/)[/]"
            )

        def _on_chunk(idx: int, total: int, chunk: DiffChunk, cache_hit: bool) -> None:
            src = "cache" if cache_hit else "AI"
            if len(chunk.file_paths) == 1:
                files_label = chunk.file_paths[0]
            elif len(chunk.file_paths) <= 3:
                files_label = ", ".join(chunk.file_paths)
            else:
                files_label = f"{chunk.file_paths[0]} (+{len(chunk.file_paths) - 1} innych)"

            if chunk.is_split:
                split_tag = (
                    f", split {chunk.split_part}/{chunk.split_total} "
                    f"grp={chunk.split_group}"
                )
            else:
                split_tag = ""

            console.print(
                f"  [dim]\u2192 Chunk {idx}/{total}[/] "
                f"[magenta]id={chunk.chunk_id}[/] "
                f"[blue]{files_label}[/] "
                f"([dim]{chunk.hunk_count} hunk(\u00f3w), {chunk.line_count}L, {src}{split_tag}[/])"
            )

        try:
            response: AgentResponse = await agent.propose_actions(
                chunked_commit=chunked_commit,
                vault_changes=vault_changes,
                vault_changed_notes=vault_changed_notes,
                vault_knowledge=knowledge,
                on_chunk_progress=_on_chunk,
            )
        except RuntimeError as exc:
            console.print(f"[red]Blad podczas wywolywania AI: {exc}[/]")
            if not ask_retry():
                return False
            continue

        if not response.actions:
            renderer.render_empty_response(response)
            renderer.info("Zaliczam commit do processed (pusty plan = commit nic nie wnosi).")
            agent.mark_commit_processed(state, project_sha=project_commit.sha, vault_commit_sha=None)
            return True

        plans: list[PlannedVaultWrite] = agent.plan_post_updates(response, knowledge)

        renderer.render_plan(response, plans)

        console.print(
            "\n[cyan]Zapisuje dokumentacje do vaulta w trybie diff-view "
            "(GREEN=nowe `[!tip]+`, RED=poprzednie `[!failure]+`)\u2026[/]"
        )
        report: ActionExecutionReport
        batch: PendingBatch
        try:
            report, batch = agent.apply_pending(response, plans)
        except Exception as exc:
            console.print(f"[red]apply_pending padlo: {exc}[/]")
            if ask_retry():
                continue
            return False

        renderer.render_execution_report(
            touched_files=report.touched_files,
            failed=[f"{o.description}: {o.error_message or 'blad'}" for o in report.failed],
        )

        if not batch.has_any_write:
            console.print(
                "[red]Nic sie nie zapisalo \u2014 wszystkie akcje i plany padly. Nie ma co akceptowac.[/]"
            )
            agent.rollback_pending(batch)
            if ask_retry():
                continue
            return False

        _render_pending_review_banner(batch, vault_path=agent.config.vault_path)

        if ask_accept_pending():
            console.print("[cyan]Sciagam diff-view (red+green) z vaulta\u2026[/]")
            rewritten = agent.finalize_pending(batch)
            if rewritten:
                console.print(f"[green]Sciagniete highlighty z {len(rewritten)} plikow.[/]")

            if not report.touched_files:
                console.print(
                    "[yellow]Po finalize nie ma czego commitowac \u2014 wszystkie akcje padly wczesniej.[/]"
                )
                if ask_retry():
                    continue
                return False

            console.print("[cyan]Commituje vault\u2026[/]")
            try:
                vault_sha = agent.commit_vault(
                    approved=True,
                    project_commit=chunked_commit.commit,
                    execution_report=report,
                    summary=response.summary,
                )
                console.print(f"[green]Zacommitowano vault: {vault_sha[:7]}[/]")
            except RuntimeError as exc:
                console.print(f"[red]Commit vaulta sie nie udal: {exc}[/]")
                console.print(
                    "[yellow]Pliki sa juz clean na dysku, ale nie trafily do Gita. "
                    "Mozesz zacommitowac recznie albo odrzucic i sprobowac ponownie.[/]"
                )
                if ask_retry():
                    continue
                return False

            agent.mark_commit_processed(
                state,
                project_sha=project_commit.sha,
                vault_commit_sha=vault_sha,
            )
            return True

        console.print("[yellow]Odrzucam \u2014 cofam vault do stanu sprzed propozycji\u2026[/]")
        restored = agent.rollback_pending(batch)
        console.print(f"[green]Przywrocono {len(restored)} plikow.[/]")
        if ask_retry():
            console.print("[cyan]Powtarzam generacje dla tego samego commita\u2026[/]\n")
            continue
        return False


async def _run() -> int:
    """Glowna korutyna \u2014 buduje agenta, iteruje po pending commitach.

    Zwraca kod wyjscia dla ``sys.exit``:

    - ``0`` \u2014 wszystko ok (rowniez "brak nowych commitow" i normalny
      koniec przy odrzuceniu)
    - ``1`` \u2014 blad krytyczny (niepoprawny config, brak remote, itd.)
    """

    project_root = Path(__file__).resolve().parent
    config_path = project_root / "config.yaml"

    if not config_path.is_file():
        console.print(f"[red]Brak pliku konfiguracyjnego: {config_path}[/]")
        console.print("[dim]Skopiuj config/config.example.yaml \u2192 config.yaml i uzupelnij sciezki.[/]")
        return 1

    try:
        agent = Agent.from_config(config_path)
    except (ValueError, RuntimeError) as exc:
        console.print(f"[red]Blad inicjalizacji agenta: {exc}[/]")
        return 1

    console.print(f"[bold]Obsidian Git Documentation Agent[/]  \u2022 provider: {agent.provider.name}")
    console.print(f"[dim]Project: {agent.config.project_repo_path}[/]")
    console.print(f"[dim]Vault:   {agent.config.vault_path}[/]\n")

    console.print("[cyan]Sync repozytoriow (pull + auto-stash)\u2026[/]")
    try:
        agent.sync_repos()
    except Exception as exc:
        console.print(f"[red]Sync repo nie powiodl sie: {exc}[/]")
        return 1

    state = agent.load_state()
    renderer = PreviewRenderer(console=console)

    seen_user_vault_commits: list[CommitInfo] = []
    seen_vault_shas: set[str] = set()

    processed_count = 0
    while True:
        commit = agent.get_next_pending_commit(state)
        if commit is None:
            break

        user_vault_commits, _ = agent.collect_vault_changes(state)
        for c in user_vault_commits:
            if c.sha not in seen_vault_shas:
                seen_vault_shas.add(c.sha)
                seen_user_vault_commits.append(c)

        proceed = await _process_single_commit(agent, state, commit, renderer)

        knowledge = agent.scan_vault()
        agent.update_vault_snapshot(state, knowledge)
        agent.save_state(state)

        if proceed:
            processed_count += 1
        else:
            console.print("\n[yellow]Zatrzymano petle na prosbe usera.[/]")
            break

    if seen_user_vault_commits:
        agent.mark_vault_user_commits_processed(state, seen_user_vault_commits)
        agent.save_state(state)

    if processed_count == 0:
        console.print("[green]Brak nowych commitow do przetworzenia. Dokumentacja jest aktualna.[/]")
    else:
        console.print(f"\n[green]Koniec biegu. Przetworzono commitow w tej sesji: {processed_count}[/]")

    return 0


def main() -> None:
    _configure_logging()
    try:
        exit_code = asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Przerwano przez uzytkownika (Ctrl+C). State zostal zapisany przy ostatniej iteracji.[/]")
        exit_code = 130
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
