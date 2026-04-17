"""CLI do przegladania logow JSONL: ``python -m logs <cmd>``.

Komendy:

- ``tail [--run LAST|<run_id>] [-n 50]`` — ostatnie N eventow z biegu
- ``list`` — lista biegow z katalogu ``logs/runs/``
- ``errors [--run LAST|<run_id>]`` — tylko eventy ``level=error``
- ``show <run_id>`` — podsumowanie jednego biegu

Bez zewnetrznych zaleznosci poza ``rich`` (juz jest w projekcie).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterator

from rich.console import Console
from rich.table import Table

DEFAULT_LOG_DIR = Path("logs/runs")


def _iter_events(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _list_runs(log_dir: Path) -> list[Path]:
    if not log_dir.is_dir():
        return []
    return sorted(
        log_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _resolve_run_path(log_dir: Path, run: str | None) -> Path | None:
    runs = _list_runs(log_dir)
    if not runs:
        return None
    if run is None or run.upper() == "LAST":
        return runs[0]
    for p in runs:
        if p.stem == run or run in p.name:
            return p
    return None


def _format_event_row(ev: dict[str, Any]) -> tuple[str, str, str, str]:
    ts = ev.get("ts", "")[:19].replace("T", " ")
    level = (ev.get("level") or "info").upper()
    etype = ev.get("type", "?")
    rest = {
        k: v
        for k, v in ev.items()
        if k not in {"ts", "level", "type", "run_id"}
    }
    summary = ", ".join(f"{k}={_short(v)}" for k, v in rest.items())
    return ts, level, etype, summary


def _short(v: Any, limit: int = 60) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str) if not isinstance(v, str) else v
    if len(s) > limit:
        return s[: limit - 3] + "..."
    return s


def cmd_list(args: argparse.Namespace, console: Console) -> int:
    runs = _list_runs(args.log_dir)
    if not runs:
        console.print(f"[yellow]Brak biegow w {args.log_dir}[/]")
        return 0

    table = Table(title=f"Biegi w {args.log_dir}")
    table.add_column("run_id", style="cyan")
    table.add_column("mtime")
    table.add_column("events", justify="right")
    table.add_column("size", justify="right")

    for p in runs[: args.limit]:
        stat = p.stat()
        events = sum(1 for _ in _iter_events(p))
        size_kb = stat.st_size / 1024
        mtime = _fmt_ts(stat.st_mtime)
        table.add_row(p.stem, mtime, str(events), f"{size_kb:.1f} KB")

    console.print(table)
    return 0


def cmd_tail(args: argparse.Namespace, console: Console) -> int:
    path = _resolve_run_path(args.log_dir, args.run)
    if path is None:
        console.print(f"[red]Nie znaleziono biegu w {args.log_dir}[/]")
        return 1

    events = list(_iter_events(path))
    events = events[-args.n :]

    table = Table(title=f"Ostatnie {len(events)} eventow z {path.stem}")
    table.add_column("ts", style="dim", no_wrap=True)
    table.add_column("lvl")
    table.add_column("type", style="cyan", no_wrap=True)
    table.add_column("payload")

    for ev in events:
        ts, level, etype, summary = _format_event_row(ev)
        lvl_style = {"ERROR": "red", "WARNING": "yellow", "INFO": "white", "DEBUG": "dim"}.get(level, "white")
        table.add_row(ts, f"[{lvl_style}]{level}[/]", etype, summary)

    console.print(table)
    console.print(f"[dim]Plik: {path}[/]")
    return 0


def cmd_errors(args: argparse.Namespace, console: Console) -> int:
    path = _resolve_run_path(args.log_dir, args.run)
    if path is None:
        console.print(f"[red]Nie znaleziono biegu w {args.log_dir}[/]")
        return 1

    errors = [ev for ev in _iter_events(path) if (ev.get("level") or "").lower() == "error"]
    if not errors:
        console.print(f"[green]Bieg {path.stem}: brak bledow[/]")
        return 0

    for ev in errors:
        ts = ev.get("ts", "")
        etype = ev.get("type", "?")
        console.print(f"[red]![/] [bold]{etype}[/] [dim]{ts}[/]")
        for k, v in ev.items():
            if k in {"type", "ts", "level", "run_id"}:
                continue
            console.print(f"  [yellow]{k}[/]: {_short(v, 200)}")
        console.print()
    return 0


def cmd_show(args: argparse.Namespace, console: Console) -> int:
    path = _resolve_run_path(args.log_dir, args.run)
    if path is None:
        console.print(f"[red]Nie znaleziono biegu '{args.run}' w {args.log_dir}[/]")
        return 1

    by_type: dict[str, int] = {}
    start_event: dict[str, Any] | None = None
    end_event: dict[str, Any] | None = None
    errors = 0

    for ev in _iter_events(path):
        by_type[ev.get("type", "?")] = by_type.get(ev.get("type", "?"), 0) + 1
        if ev.get("type") == "run.started" and start_event is None:
            start_event = ev
        if ev.get("type") == "run.ended":
            end_event = ev
        if (ev.get("level") or "").lower() == "error":
            errors += 1

    console.print(f"[bold]Bieg {path.stem}[/]")
    console.print(f"  plik: {path}")
    if start_event:
        console.print(
            f"  provider: {start_event.get('provider')} / model: {start_event.get('model')} "
            f"/ effort: {start_event.get('effort')}"
        )
    if end_event:
        console.print(
            f"  exit: {end_event.get('exit_code')}  processed: {end_event.get('processed_count')}  "
            f"duration: {end_event.get('duration_s', 0):.1f}s"
        )
    console.print(f"  errors: [{'red' if errors else 'green'}]{errors}[/]")

    table = Table(title="Typy eventow")
    table.add_column("type", style="cyan")
    table.add_column("count", justify="right")
    for t, c in sorted(by_type.items(), key=lambda kv: -kv[1]):
        table.add_row(t, str(c))
    console.print(table)
    return 0


def _fmt_ts(mtime: float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m logs",
        description="Przegladanie logow biegow agenta (JSONL).",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help=f"Katalog z plikami biegow (domyslnie {DEFAULT_LOG_DIR}).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="Lista biegow")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    p_tail = sub.add_parser("tail", help="Ostatnie N eventow biegu")
    p_tail.add_argument("--run", default="LAST", help="run_id albo LAST")
    p_tail.add_argument("-n", type=int, default=50)
    p_tail.set_defaults(func=cmd_tail)

    p_err = sub.add_parser("errors", help="Pokaz tylko eventy level=error")
    p_err.add_argument("--run", default="LAST", help="run_id albo LAST")
    p_err.set_defaults(func=cmd_errors)

    p_show = sub.add_parser("show", help="Podsumowanie biegu")
    p_show.add_argument("run", help="run_id albo LAST")
    p_show.set_defaults(func=cmd_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console()
    return args.func(args, console)


if __name__ == "__main__":
    sys.exit(main())
