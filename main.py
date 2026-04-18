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

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from logs import RunLogger, configure_stdlib_logging
from src.agent import (
    Agent,
    PendingBatch,
    PreviewRenderer,
    ProposedPlan,
    SessionResult,
    ask_accept_pending,
    ask_retry,
)
from src.agent.action_executor import ActionExecutionReport
from src.agent.models_chunks import ChunkedCommit, DiffChunk
from src.agent.moc_agent import MOCAgent, MOCSessionResult
from src.agent.moc_planner import PlannedVaultWrite
from src.git.models import CommitInfo

logger = logging.getLogger(__name__)
console = Console()

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def _load_logs_config(config_path: Path) -> dict[str, object]:
    """Czyta sekcje ``logs:`` z YAML. Brak -> pusty dict.

    Defensywnie — gdy configu nie ma albo jest uszkodzony, zwraca ``{}``
    i pozwala main() wyswietlic wlasny komunikat o brakujacym configu.
    """
    if not config_path.is_file():
        return {}
    try:
        import yaml
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    logs_cfg = raw.get("logs") if isinstance(raw, dict) else None
    return logs_cfg if isinstance(logs_cfg, dict) else {}


def _resolve_log_dir(logs_cfg: dict[str, object]) -> Path:
    """Buduje absolutny katalog logow z configu (default ``logs/runs``)."""
    raw = logs_cfg.get("dir") if isinstance(logs_cfg, dict) else None
    if isinstance(raw, str) and raw.strip():
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        return candidate
    return PROJECT_ROOT / "logs" / "runs"


def _level_from_config(raw: object, fallback: int) -> int:
    """Mapuje stringa ``"INFO"`` / ``"WARNING"`` itp. na wartosc z ``logging``."""
    if isinstance(raw, str):
        level = logging.getLevelName(raw.strip().upper())
        if isinstance(level, int):
            return level
    if isinstance(raw, int):
        return raw
    return fallback


def _setup_logging(logs_cfg: dict[str, object], log_dir: Path) -> None:
    """Konfiguracja stdlib logging — poziomy z ``config.yaml -> logs``.

    JSONL leci osobno przez ``RunLogger`` (structured events).
    Ten setup zajmuje sie tylko klasycznymi ``logger.info/warning/error``.
    """

    stdlib_level = _level_from_config(logs_cfg.get("stdlib_level"), logging.INFO)
    console_level = _level_from_config(logs_cfg.get("console_level"), logging.WARNING)

    configure_stdlib_logging(
        level=stdlib_level,
        console_level=console_level,
        log_dir=log_dir,
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
    run_logger: RunLogger,
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

        run_logger.log_commit_started(
            sha=project_commit.sha,
            author=project_commit.author,
            subject=subject,
        )

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

            run_logger.log_chunk(
                sha=project_commit.sha,
                chunk_id=chunk.chunk_id,
                chunk_idx=idx,
                chunk_total=total,
                files=list(chunk.file_paths),
                hunk_count=chunk.hunk_count,
                line_count=chunk.line_count,
                cache_hit=cache_hit,
            )

        try:
            session: SessionResult = await agent.run_session(
                chunked_commit=chunked_commit,
                vault_changes=vault_changes,
                vault_changed_notes=vault_changed_notes,
                vault_knowledge=knowledge,
                on_chunk_progress=_on_chunk,
            )
            plan: ProposedPlan = session.plan
        except RuntimeError as exc:
            console.print(f"[red]Blad podczas wywolywania AI: {exc}[/]")
            run_logger.log(
                f"AI call nie powiodlo sie: {exc}",
                level="error",
                sha=project_commit.sha,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            if not ask_retry():
                run_logger.log_commit_rejected(sha=project_commit.sha, reason="user_abort_after_ai_error")
                return False
            continue

        if not plan.writes:
            renderer.render_empty_response(plan)
            renderer.info("Zaliczam commit do processed (pusty plan = commit nic nie wnosi).")
            agent.mark_commit_processed(state, project_sha=project_commit.sha, vault_commit_sha=None)
            run_logger.log_commit_processed(sha=project_commit.sha, vault_sha=None)
            return True

        plans: list[PlannedVaultWrite] = agent.plan_post_updates(plan, knowledge)

        renderer.render_plan(plan, plans)

        console.print(
            "\n[cyan]Zapisuje dokumentacje do vaulta w trybie diff-view "
            "(GREEN=nowe `[!tip]+`, RED=poprzednie `[!failure]+`)\u2026[/]"
        )
        report: ActionExecutionReport
        batch: PendingBatch
        try:
            report, batch = agent.apply_pending(plan, plans)
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
            run_logger.log_pending(approved=True, files=len(batch.clean_by_path))
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
                    summary=plan.summary,
                )
                console.print(f"[green]Zacommitowano vault: {vault_sha[:7]}[/]")
                run_logger.log_vault_commit(sha=vault_sha)
            except RuntimeError as exc:
                console.print(f"[red]Commit vaulta sie nie udal: {exc}[/]")
                run_logger.log(
                    f"vault commit failed: {exc}",
                    level="error",
                    sha=project_commit.sha,
                )
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
            run_logger.log_commit_processed(sha=project_commit.sha, vault_sha=vault_sha)
            return True

        run_logger.log_pending(approved=False, files=len(batch.clean_by_path))
        console.print("[yellow]Odrzucam \u2014 cofam vault do stanu sprzed propozycji\u2026[/]")
        restored = agent.rollback_pending(batch)
        console.print(f"[green]Przywrocono {len(restored)} plikow.[/]")
        if ask_retry():
            console.print("[cyan]Powtarzam generacje dla tego samego commita\u2026[/]\n")
            continue
        run_logger.log_commit_rejected(sha=project_commit.sha, reason="user_rejected_pending")
        return False


async def _run(run_logger: RunLogger, *, moc_only: bool = False) -> int:
    """Glowna korutyna \u2014 buduje agenta, iteruje po pending commitach.

    Zwraca kod wyjscia dla ``sys.exit``:

    - ``0`` \u2014 wszystko ok (rowniez "brak nowych commitow" i normalny
      koniec przy odrzuceniu)
    - ``1`` \u2014 blad krytyczny (niepoprawny config, brak remote, itd.)
    """

    config_path = CONFIG_PATH

    if not config_path.is_file():
        console.print(f"[red]Brak pliku konfiguracyjnego: {config_path}[/]")
        console.print("[dim]Skopiuj config/config.example.yaml \u2192 config.yaml i uzupelnij sciezki.[/]")
        run_logger.log("missing_config", level="error", path=str(config_path))
        return 1

    try:
        agent = Agent.from_config(config_path, run_logger=run_logger)
    except (ValueError, RuntimeError) as exc:
        console.print(f"[red]Blad inicjalizacji agenta: {exc}[/]")
        run_logger.log(
            f"init_failed: {exc}",
            level="error",
            error_type=type(exc).__name__,
        )
        return 1

    console.print(f"[bold]Obsidian Git Documentation Agent[/]  \u2022 provider: {agent.provider.name}")
    console.print(f"[dim]Project: {agent.config.project_repo_path}[/]")
    console.print(f"[dim]Vault:   {agent.config.vault_path}[/]\n")

    run_logger.log_run_started(
        provider=agent.provider.name,
        model=getattr(agent.provider, "default_model", "?"),
        effort=_read_effort_from_config(config_path, agent.provider.name),
        project_repo=str(agent.config.project_repo_path),
        vault=str(agent.config.vault_path),
    )

    if agent.mcp_settings.enabled:
        try:
            await agent.start_mcp()
        except RuntimeError as exc:
            console.print(f"[red]MCP runtime nie wstal: {exc}[/]")
            run_logger.log(
                f"mcp_start_failed: {exc}",
                level="error",
                error_type=type(exc).__name__,
            )
            return 1
        console.print(
            f"[dim]MCP: [cyan]{agent.mcp_settings.url}[/] "
            f"(server='{agent.mcp_settings.server_name}')[/]"
        )

    try:
        if moc_only:
            return await _run_moc_only(agent, run_logger)
        return await _run_agent_loop(agent, run_logger)
    finally:
        try:
            await agent.stop_mcp()
        except Exception as exc:
            logger.warning("stop_mcp podczas cleanupu padlo: %r", exc)


async def _run_moc_only(agent: "Agent", run_logger: RunLogger) -> int:
    """Osobny flow - odpala wylacznie MOCAgenta (bez dokumentowania commitow).

    Flow:

    1. Sync vault (pull) - zeby nie wejsc w konflikt jesli user cos dopisal recznie.
    2. Ensure bootstrap MOC - MOCAgent zaklada ze root-MOC istnieje.
    3. Uruchom ``MOCAgent.run_session`` z trigger_context = "flaga --moc-only".
    4. Jesli session zwrocil jakies pisy - preview + user approval + commit
       pod prefiksem ``Agent-MOC:``.
    5. Jesli audyt byl czysty (no writes) - informacja w konsoli, zero commita.
    """

    console.print("[bold cyan]TRYB MOC-ONLY[/] \u2014 MOCAgent audytuje MOC i uzupelnia strukture.\n")

    console.print("[cyan]Sync vault (pull + auto-stash)\u2026[/]")
    try:
        agent.git_vault_syncer.sync()
    except Exception as exc:
        console.print(f"[red]Sync vault nie powiodl sie: {exc}[/]")
        run_logger.log(
            f"sync_vault_failed: {exc}",
            level="error",
            error_type=type(exc).__name__,
        )
        return 1

    try:
        bootstrap_outcome = agent.ensure_bootstrap_moc()
    except RuntimeError as exc:
        console.print(f"[red]Bootstrap MOC nie powiodl sie: {exc}[/]")
        return 1
    if bootstrap_outcome is not None and bootstrap_outcome.result in ("created", "merged"):
        count = len(bootstrap_outcome.added_links)
        label = "utworzony" if bootstrap_outcome.result == "created" else "zmergowany"
        console.print(
            f"[green]Bootstrap MOC {label}: {bootstrap_outcome.path} (+{count} linkow).[/]"
        )

    try:
        moc_agent = MOCAgent.from_agent(agent)
    except ValueError as exc:
        console.print(f"[red]Blad konfiguracji MOCAgenta: {exc}[/]")
        return 1

    if not moc_agent.config.enabled:
        console.print("[yellow]MOCAgent wylaczony w config.yaml (moc.enabled=false). Nic nie robie.[/]")
        return 0

    return await _run_moc_agent(
        agent,
        moc_agent,
        run_logger,
        trigger_context="uruchomienie flaga --moc-only",
    )


async def _run_moc_agent(
    agent: "Agent",
    moc_agent: "MOCAgent",
    run_logger: RunLogger,
    *,
    trigger_context: str,
) -> int:
    """Pelen cykl sesji MOCAgenta: audyt + akcje + preview + approval + commit.

    Wydzielone zeby moglo byc wolane zarowno z ``--moc-only`` jak i jako
    delegacja po zakonczeniu doc-loopu (``moc.delegate_after_docs=true``).
    Zwraca ``0`` przy sukcesie (z commitem albo bez zmian), ``1`` przy
    bledzie ktory user powinien zobaczyc.
    """

    console.print(Panel.fit(
        "[bold]MOCAgent sesja[/]\n"
        f"[dim]trigger: {trigger_context}[/]\n"
        f"[dim]MOC: {moc_agent.config.moc_path}[/]",
        border_style="cyan",
    ))

    run_logger.log("moc_agent_started", trigger=trigger_context, moc_path=moc_agent.config.moc_path)

    try:
        session = await moc_agent.run_session(trigger_context=trigger_context)
    except RuntimeError as exc:
        console.print(f"[red]MOCAgent padl: {exc}[/]")
        run_logger.log(
            f"moc_agent_failed: {exc}",
            level="error",
            error_type=type(exc).__name__,
        )
        return 1

    run_logger.log(
        "moc_agent_session_done",
        iterations=session.iterations_used,
        tool_calls=session.tool_calls_count,
        finalized=session.finalized_by_submit_plan,
        has_changes=session.has_changes,
    )

    if session.plan is None:
        console.print(f"[dim]MOCAgent pominiety: {session.skipped_reason or 'brak planu'}.[/]")
        return 0

    if not session.has_changes:
        console.print(
            f"[green]MOCAgent: audyt czysty, brak zmian.[/] "
            f"[dim](iteracji: {session.iterations_used}, tool-calls: {session.tool_calls_count})[/]"
        )
        console.print(f"[dim]Summary: {session.plan.summary}[/]")
        return 0

    renderer = PreviewRenderer(console=console)
    report, batch = moc_agent.apply_pending(session.plan)
    renderer.render_plan(session.plan, [])

    console.print(
        f"\n[dim]MOCAgent: {session.iterations_used} iteracji, "
        f"{session.tool_calls_count} tool-calls, "
        f"{len(session.plan.writes)} pisow zaproponowanych.[/]\n"
    )

    approved = ask_accept_pending()
    if not approved:
        console.print("[yellow]Rollback - vault cofniety do stanu sprzed MOCAgenta.[/]")
        moc_agent.rollback_pending(batch)
        run_logger.log("moc_agent_rejected", writes=len(session.plan.writes))
        return 0

    moc_agent.finalize_pending(batch)
    try:
        sha = moc_agent.commit_vault_moc(
            approved=True,
            execution_report=report,
            summary=session.plan.summary,
        )
    except RuntimeError as exc:
        console.print(f"[red]Commit MOC padl: {exc}[/]")
        run_logger.log(f"moc_commit_failed: {exc}", level="error")
        return 1

    console.print(
        f"[green]Commit MOC: [cyan]{sha[:7]}[/] - {len(report.touched_files)} plikow zmienione.[/]"
    )
    run_logger.log(
        "moc_agent_commit",
        sha=sha,
        touched=len(report.touched_files),
        summary=session.plan.summary,
    )
    return 0


async def _run_agent_loop(agent: "Agent", run_logger: RunLogger) -> int:
    """Wewnetrzna petla po tym jak MCP wstalo - wyciagniete dla try/finally cleanupu."""

    console.print("[cyan]Sync repozytoriow (pull + auto-stash)\u2026[/]")
    try:
        agent.sync_repos()
    except Exception as exc:
        console.print(f"[red]Sync repo nie powiodl sie: {exc}[/]")
        run_logger.log(
            f"sync_failed: {exc}",
            level="error",
            error_type=type(exc).__name__,
        )
        return 1

    try:
        bootstrap_outcome = agent.ensure_bootstrap_moc()
    except RuntimeError as exc:
        console.print(f"[red]Bootstrap MOC nie powiodl sie: {exc}[/]")
        run_logger.log(
            f"bootstrap_moc_failed: {exc}",
            level="error",
            error_type=type(exc).__name__,
        )
        return 1

    if bootstrap_outcome is not None:
        count = len(bootstrap_outcome.added_links)
        if bootstrap_outcome.result == "created":
            console.print(
                f"[green]Bootstrap MOC utworzony: {bootstrap_outcome.path} "
                f"(+{count} linkow z vaulta)[/] "
                f"[dim](label: {bootstrap_outcome.label})[/]"
            )
        elif bootstrap_outcome.result == "merged":
            console.print(
                f"[green]Bootstrap MOC zmergowany: {bootstrap_outcome.path} "
                f"(+{count} brakujacych linkow)[/]"
            )
        elif bootstrap_outcome.result == "is_not_a_moc":
            console.print(
                f"[yellow]Bootstrap MOC pominiety: {bootstrap_outcome.path} "
                "istnieje ale nie jest MOC-iem \u2014 sprawdz frontmatter.[/]"
            )
        else:
            console.print(
                f"[dim]Bootstrap MOC: {bootstrap_outcome.path} \u2014 {bootstrap_outcome.result} (zero zmian).[/]"
            )

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

        proceed = await _process_single_commit(agent, state, commit, renderer, run_logger)

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

    try:
        moc_agent = MOCAgent.from_agent(agent)
    except ValueError as exc:
        console.print(f"[yellow]MOCAgent nieaktywny (blad configu): {exc}[/]")
        return 0

    if not moc_agent.config.enabled or not moc_agent.config.delegate_after_docs:
        return 0

    trigger = (
        f"delegacja po doc-agencie (przetworzono {processed_count} commitow)"
        if processed_count > 0
        else "delegacja po doc-agencie (brak nowych commitow)"
    )
    console.print("\n[bold cyan]---> Delegacja do MOCAgenta[/] [dim](moc.delegate_after_docs=true)[/]")
    try:
        return await _run_moc_agent(agent, moc_agent, run_logger, trigger_context=trigger)
    except Exception as exc:
        console.print(f"[yellow]MOCAgent padl ale doc-agent skonczyl - kontynuuje: {exc}[/]")
        run_logger.log(f"moc_agent_delegation_failed: {exc}", level="warning")
        return 0


def _read_effort_from_config(config_path: Path, provider_name: str) -> str | None:
    """Best-effort — czytamy effort tylko po to zeby wrzucic do run.started.

    Nie chcemy tu powielac walidacji z ``build_provider`` — jesli cokolwiek
    padnie, zwracamy None i puszczamy dalej.
    """
    try:
        import yaml
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        section = (cfg.get("providers") or {}).get(provider_name) or {}
        effort = section.get("effort")
        return str(effort) if isinstance(effort, str) else None
    except Exception:
        return None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parsuje argv. Wspierane flagi:

    - ``--moc-only`` — pomija dokumentowanie commitow projektu i odpala
      tylko MOCAgenta (audyt + rozbudowa MOC).
    """

    parser = argparse.ArgumentParser(
        prog="obsagent",
        description=(
            "Obsidian Git Documentation Agent. Bez flag: dokumentuje nowe "
            "commity projektu; z --moc-only: odpala tylko MOCAgenta (audyt "
            "+ rozbudowa MOC, bez dokumentowania)."
        ),
    )
    parser.add_argument(
        "--moc-only",
        action="store_true",
        help=(
            "Pomin dokumentowanie commitow - odpal tylko MOCAgenta. "
            "Uzywaj gdy vault jest juz w pelni udokumentowany a chcesz "
            "odswiezyc strukture nawigacyjna MOC (huby, technologie, koncepty)."
        ),
    )
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args()
    load_dotenv()

    logs_cfg = _load_logs_config(CONFIG_PATH)
    log_dir = _resolve_log_dir(logs_cfg)

    _setup_logging(logs_cfg, log_dir)

    project_name = PROJECT_ROOT.name
    verbose_cfg = bool(logs_cfg.get("verbose")) if isinstance(logs_cfg, dict) else False
    env_verbose = os.environ.get("OBSAGENT_LOG_VERBOSE") == "1"
    run_logger = RunLogger.create(
        log_dir=log_dir,
        project_name=project_name,
        console_verbose=verbose_cfg or env_verbose,
    )
    console.print(
        f"[dim]Logs: run_id=[cyan]{run_logger.run_id}[/] \u2192 {run_logger.jsonl_path}[/]"
    )

    exit_code = 0
    try:
        exit_code = asyncio.run(_run(run_logger, moc_only=args.moc_only))
    except KeyboardInterrupt:
        console.print("\n[yellow]Przerwano przez uzytkownika (Ctrl+C). State zostal zapisany przy ostatniej iteracji.[/]")
        run_logger.log("interrupted", level="warning")
        exit_code = 130
    except Exception as exc:
        console.print(f"\n[red]Niezlapany wyjatek: {exc!r}[/]")
        run_logger.log(
            f"uncaught: {exc!r}",
            level="error",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        exit_code = 1
    finally:
        run_logger.log_run_ended(exit_code=exit_code)
        run_logger.close()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
