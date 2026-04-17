"""Preview UI \u2014 Rich-formatted tabela akcji + plan\u00f3w przed ``[T/n]``.

User musi zobaczyc **cale** zmiany, ktore znajda sie w commicie na vault:

- akcje od AI (create / update / append)
- auto-plan MOC (dopisanie linkow do map of content)
- auto-plan indeksu (dopisanie wpisu do ``_index.md``)

Kazda pozycja wysiwetlana w tabeli Rich z trzema kolumnami: typ, sciezka,
opis (pierwsze N linii nowej tresci lub podsumowanie). Pod tabela
``AgentResponse.summary`` od AI \u2014 krotki opis dlaczego AI wybralo te akcje.

Na koncu prompt ``[T/n]`` + obsluga odpowiedzi. Funkcja ``ask_confirm``
blokujaco czyta stdin \u2014 zwraca bool. User nacisnie Enter bez tekstu =
odrzucenie (bezpieczny default).
"""

from __future__ import annotations

import sys
from typing import Iterable

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from src.agent.models_actions import AgentAction, AgentResponse
from src.agent.moc_planner import PlannedVaultWrite
from src.git.models import CommitInfo


_PREVIEW_CONTENT_LINES = 12
"""Ile linii tresci nowego pliku pokazywac w panelu pod tabela."""


class PreviewRenderer:
    """Encapsulate Rich output \u2014 jedna instancja Console per bieg agenta.

    Kwargs ``console`` pozwala wstrzyknac wlasna Console w testach
    (np. do bufora, bez faktycznego stdout).
    """

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    def render_commit_header(self, commit: CommitInfo, *, iteration: int, total: int) -> None:
        """Naglowek przed preview \u2014 informuje do ktorego commita bierzemy sie teraz."""

        first_msg = commit.message.strip().split("\n", 1)[0][:100]
        self.console.print()
        self.console.rule(
            f"[bold cyan]Iteracja {iteration}/{total} \u2014 "
            f"commit {commit.sha[:7]}[/bold cyan]"
        )
        self.console.print(
            Panel.fit(
                Text.assemble(
                    (f"{first_msg}\n", "bold white"),
                    (f"autor: {commit.author}   ", "dim"),
                    (f"data: {commit.date.isoformat()}\n", "dim"),
                    (f"+{commit.stats.insertions} / -{commit.stats.deletions}   ", "green"),
                    (f"{len(commit.changes)} plikow", "yellow"),
                ),
                title=f"{commit.sha}",
                border_style="cyan",
            )
        )

    def render_empty_response(self, response: AgentResponse) -> None:
        """AI zwrocilo pusta liste akcji \u2014 informujemy usera i nie pytamy o [T/n]."""

        self.console.print()
        self.console.print(
            Panel(
                Text.assemble(
                    ("AI uznal, ze ten commit nie wymaga dokumentacji.\n\n", "yellow"),
                    ("Podsumowanie: ", "bold"),
                    (response.summary, "white"),
                ),
                title="Brak akcji",
                border_style="yellow",
            )
        )

    def render_plan(
        self,
        response: AgentResponse,
        plans: list[PlannedVaultWrite],
    ) -> None:
        """Wyswietla tabele akcji + planow i panele z tresciami."""

        self.console.print()
        self.console.print(Panel(response.summary, title="Podsumowanie AI", border_style="cyan"))
        self.console.print()

        table = Table(title="Plan zmian do zapisu i commita", show_lines=True)
        table.add_column("#", justify="right", style="dim", width=3)
        table.add_column("Typ", style="bold magenta")
        table.add_column("Sciezka", style="bold white")
        table.add_column("Opis", style="white")

        row_idx = 1
        for action in response.actions:
            type_style = _action_type_style(action.type)
            description = _action_description(action)
            table.add_row(
                str(row_idx),
                Text(action.type.upper(), style=type_style),
                action.path,
                description,
            )
            row_idx += 1

        if plans:
            for plan in plans:
                style = _plan_kind_style(plan.kind)
                desc = plan.preview_lines[0] if plan.preview_lines else plan.reason
                if len(plan.preview_lines) > 1:
                    desc += f" (+{len(plan.preview_lines) - 1} wiecej)"
                table.add_row(
                    str(row_idx),
                    Text(plan.kind.upper(), style=style),
                    plan.path,
                    desc,
                )
                row_idx += 1

        self.console.print(table)
        self.console.print()

        if response.actions:
            self._render_action_contents(response.actions)

        if plans:
            self._render_plan_details(plans)

    def _render_action_contents(self, actions: Iterable[AgentAction]) -> None:
        self.console.print(Text("Tresc akcji (pierwsze linie):", style="bold"))
        self.console.print()
        for action in actions:
            preview = _truncate_content(action.content, _PREVIEW_CONTENT_LINES)
            title = f"[{action.type.upper()}] {action.path}"
            syntax = Syntax(
                preview,
                "markdown",
                theme="monokai",
                line_numbers=False,
                word_wrap=True,
            )
            self.console.print(Panel(syntax, title=title, border_style=_action_type_style(action.type)))

    def _render_plan_details(self, plans: Iterable[PlannedVaultWrite]) -> None:
        self.console.print()
        self.console.print(Text("Auto-plan MOC / indeksu:", style="bold"))
        self.console.print()
        for plan in plans:
            if plan.preview_lines:
                body = "\n".join(f"\u2022 {line}" for line in plan.preview_lines)
            else:
                body = plan.reason
            self.console.print(
                Panel(
                    body,
                    title=f"[{plan.kind.upper()}] {plan.path}",
                    border_style=_plan_kind_style(plan.kind),
                )
            )

    def render_execution_report(self, touched_files: list[str], failed: list[str]) -> None:
        """Krotki raport po wykonaniu (sukcesy / bledy) \u2014 przed commitem."""

        self.console.print()
        if not failed:
            self.console.print(
                f"[green]Zapisano {len(touched_files)} plikow.[/green]"
            )
        else:
            self.console.print(
                f"[yellow]Zapisano {len(touched_files)} plikow, "
                f"{len(failed)} akcji sie nie udalo:[/yellow]"
            )
            for err in failed:
                self.console.print(f"  [red]\u2717[/red] {err}")

    def info(self, message: str) -> None:
        self.console.print(f"[blue]\u2139[/blue]  {message}")

    def warn(self, message: str) -> None:
        self.console.print(f"[yellow]\u26a0[/yellow]  {message}")

    def error(self, message: str) -> None:
        self.console.print(f"[red]\u2717[/red] {message}")

    def success(self, message: str) -> None:
        self.console.print(f"[green]\u2713[/green] {message}")


def ask_confirm(prompt: str = "Zatwierdz i zacommituj?", *, default_no: bool = True) -> bool:
    """Blokujace pytanie [T/n] na stdin. Enter = odmowa (bezpieczny default).

    :param default_no: gdy True (domyslnie), pusty input znaczy 'n'.
        Ustawienie False odwraca default (pusty = T) \u2014 rezerwujemy
        na automatyczne scenariusze, nie do zwyklego flow.
    :return: True jesli user potwierdzil (T / t / tak / y / yes),
        False w kazdym innym przypadku.
    """

    suffix = "[t/N]" if default_no else "[T/n]"
    raw = input(f"{prompt} {suffix} ").strip().lower()

    if not raw:
        return not default_no

    return raw in {"t", "tak", "y", "yes", "true", "1"}


def ask_retry() -> bool:
    """Po odmowie [n] pyta: 'sprobowac jeszcze raz wygenerowac dokumentacje?'.

    Domyslnie NIE (Enter = stop calkowity). True = ponowny call AI dla
    tego samego commita, False = konczymy bieg bez zapisu.
    """

    return ask_confirm(
        prompt="Sprobowac jeszcze raz wygenerowac dokumentacje dla tego commita?",
        default_no=True,
    )


def _action_type_style(action_type: str) -> str:
    if action_type == "create":
        return "bold green"
    if action_type == "update":
        return "bold yellow"
    if action_type == "append":
        return "bold blue"
    return "white"


def _plan_kind_style(kind: str) -> str:
    if kind == "moc_append":
        return "magenta"
    if kind.startswith("index"):
        return "cyan"
    return "white"


def _action_description(action: AgentAction) -> str:
    lines = action.content.count("\n") + (1 if action.content and not action.content.endswith("\n") else 0)
    first_line = action.content.splitlines()[0] if action.content else ""
    if first_line.startswith("---"):
        first_line = "(frontmatter + ...)"
    return f"{lines} linii, start: {first_line[:60]!r}" if first_line else f"{lines} linii"


def _truncate_content(content: str, limit: int) -> str:
    lines = content.splitlines()
    if len(lines) <= limit:
        return content
    head = "\n".join(lines[:limit])
    return f"{head}\n... ({len(lines) - limit} kolejnych linii ukryto)"


def _force_flush() -> None:
    """Wymusza wysylkee buforow stdout przed ``input()`` \u2014 defensywne."""

    try:
        sys.stdout.flush()
    except Exception:
        pass
