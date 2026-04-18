"""Preview UI \u2014 Rich-formatted tabela akcji + plan\u00f3w przed ``[T/n]``.

User musi zobaczyc **cale** zmiany, ktore znajda sie w commicie na vault:

- akcje od AI (create / update / append)
- auto-plan MOC (dopisanie linkow do map of content)
- auto-plan indeksu (dopisanie wpisu do ``_index.md``)

Kazda pozycja wysiwetlana w tabeli Rich z trzema kolumnami: typ, sciezka,
opis (pierwsze N linii nowej tresci lub podsumowanie). Pod tabela
``ProposedPlan.summary`` od AI \u2014 krotki opis dlaczego AI wybralo te akcje.

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

from src.agent.models_actions import ProposedPlan, ProposedWrite
from src.agent.moc_planner import PlannedVaultWrite
from src.agent.tools.vault_write.register_pending_concept import (
    PENDING_CONCEPTS_PATH,
    PENDING_CONCEPTS_SECTION,
)
from src.agent.tools.vault_write._markdown_ops import (
    _iter_code_fence_mask,
    _parse_pipe_row,
    _split_lines_preserving,
    _TABLE_SEPARATOR_RE,
    find_heading_span,
)
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

    def render_empty_response(self, plan: ProposedPlan) -> None:
        """AI zwrocilo pusta liste akcji \u2014 informujemy usera i nie pytamy o [T/n]."""

        self.console.print()
        self.console.print(
            Panel(
                Text.assemble(
                    ("AI uznal, ze ten commit nie wymaga dokumentacji.\n\n", "yellow"),
                    ("Podsumowanie: ", "bold"),
                    (plan.summary, "white"),
                ),
                title="Brak akcji",
                border_style="yellow",
            )
        )

    def render_plan(
        self,
        plan: ProposedPlan,
        plans: list[PlannedVaultWrite],
    ) -> None:
        """Wyswietla tabele pisow + planow i panele z tresciami.

        Pisy na ``_Pending_Concepts.md`` (Faza 6) sa wyjmowane z glownej
        tabeli i renderowane w dedykowanym panelu "Placeholdery (pending
        concepts)" \u2014 to notatka-sluga, indeks, nie wezel merytoryczny,
        wiec user ma ja jako osobny kontekst (a nie mieszana z ADR-ami i
        modulami).
        """

        self.console.print()
        self.console.print(Panel(plan.summary, title="Podsumowanie AI", border_style="cyan"))
        self.console.print()

        pending_concept_writes: list[ProposedWrite] = []
        regular_writes: list[ProposedWrite] = []
        for write in plan.writes:
            if write.path == PENDING_CONCEPTS_PATH:
                pending_concept_writes.append(write)
            else:
                regular_writes.append(write)

        table = Table(title="Plan zmian do zapisu i commita", show_lines=True)
        table.add_column("#", justify="right", style="dim", width=3)
        table.add_column("Typ", style="bold magenta")
        table.add_column("Sciezka", style="bold white")
        table.add_column("Opis", style="white")

        row_idx = 1
        for write in regular_writes:
            type_style = _action_type_style(write.type)
            description = _action_description(write)
            table.add_row(
                str(row_idx),
                Text(write.type.upper(), style=type_style),
                write.path,
                description,
            )
            row_idx += 1

        if plans:
            for planned in plans:
                style = _plan_kind_style(planned.kind)
                desc = planned.preview_lines[0] if planned.preview_lines else planned.reason
                if len(planned.preview_lines) > 1:
                    desc += f" (+{len(planned.preview_lines) - 1} wiecej)"
                table.add_row(
                    str(row_idx),
                    Text(planned.kind.upper(), style=style),
                    planned.path,
                    desc,
                )
                row_idx += 1

        self.console.print(table)
        self.console.print()

        if regular_writes:
            self._render_action_contents(regular_writes)

        if plans:
            self._render_plan_details(plans)

        if pending_concept_writes:
            self._render_pending_concepts(pending_concept_writes)

    def _render_action_contents(self, writes: Iterable[ProposedWrite]) -> None:
        self.console.print(Text("Tresc akcji (pierwsze linie):", style="bold"))
        self.console.print()
        for write in writes:
            preview = _truncate_content(write.content, _PREVIEW_CONTENT_LINES)
            title = f"[{write.type.upper()}] {write.path}"
            syntax = Syntax(
                preview,
                "markdown",
                theme="monokai",
                line_numbers=False,
                word_wrap=True,
            )
            self.console.print(Panel(syntax, title=title, border_style=_action_type_style(write.type)))

    def _render_plan_details(self, plans: Iterable[PlannedVaultWrite]) -> None:
        self.console.print()
        self.console.print(Text("Auto-plan MOC / indeksu:", style="bold"))
        self.console.print()
        for planned in plans:
            if planned.preview_lines:
                body = "\n".join(f"\u2022 {line}" for line in planned.preview_lines)
            else:
                body = planned.reason
            self.console.print(
                Panel(
                    body,
                    title=f"[{planned.kind.upper()}] {planned.path}",
                    border_style=_plan_kind_style(planned.kind),
                )
            )

    def _render_pending_concepts(self, writes: Iterable[ProposedWrite]) -> None:
        """Dedykowany panel dla pisow na ``_Pending_Concepts.md`` (Faza 6).

        Parsuje tabele ``## Placeholdery`` z finalnej tresci pisu i
        pokazuje userowi liste zarejestrowanych (lub zaktualizowanych)
        placeholderow \u2014 bez duzego diffa ADR/hub-stylowego. Gdy z
        jakichs powodow parsing padnie (niespojny markdown), fallbackujemy
        do skroconego podgladu tresci.
        """

        writes_list = list(writes)
        if not writes_list:
            return

        self.console.print()
        self.console.print(Text("Placeholdery (pending concepts):", style="bold"))
        self.console.print()

        for write in writes_list:
            rows = _extract_pending_concept_rows(write.content)
            title = f"[{write.type.upper()}] {write.path}"
            if rows:
                body_lines = [
                    f"\u2022 [bold]{name}[/bold]"
                    + (f" \u2014 [dim]{', '.join(sources)}[/dim]" if sources else "")
                    + (f"\n  [italic dim]{hint}[/italic dim]" if hint else "")
                    for name, sources, hint in rows
                ]
                body = "\n".join(body_lines)
                subtitle = f"{len(rows)} wpis(y) w indeksie placeholderow"
                self.console.print(
                    Panel(
                        body,
                        title=title,
                        subtitle=subtitle,
                        border_style="magenta",
                    )
                )
            else:
                preview = _truncate_content(write.content, _PREVIEW_CONTENT_LINES)
                syntax = Syntax(
                    preview,
                    "markdown",
                    theme="monokai",
                    line_numbers=False,
                    word_wrap=True,
                )
                self.console.print(
                    Panel(
                        syntax,
                        title=title,
                        subtitle="(nie udalo sie sparsowac tabeli \u2014 pelny podglad)",
                        border_style="magenta",
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


_YES_TOKENS = {"t", "tak", "y", "yes", "true", "1"}
_NO_TOKENS = {"n", "nie", "no", "false", "0"}


def ask_confirm(prompt: str = "Zatwierdz i zacommituj?", *, default_no: bool = True) -> bool:
    """Blokujace pytanie [T/n] na stdin. Enter = default (bezpieczny default_no).

    Akceptuje tylko ``t``/``n`` (oraz aliasy: ``tak``/``nie``, ``yes``/``no``,
    ``1``/``0``, ``true``/``false``). Kazda inna odpowiedz drukuje komunikat
    bledu i petla pyta ponownie — **nic w vaulcie/na dysku nie jest
    modyfikowane** w trakcie oczekiwania na poprawna odpowiedz, bo ta
    funkcja tylko czyta stdin.

    :param default_no: gdy True (domyslnie), pusty input znaczy 'n'.
        Ustawienie False odwraca default (pusty = T) \u2014 rezerwujemy
        na automatyczne scenariusze, nie do zwyklego flow.
    :return: True jesli user potwierdzil (t / tak / y / yes / 1 / true),
        False jesli odrzucil (n / nie / no / 0 / false) lub nacisnal Enter
        przy ``default_no=True``.
    """

    suffix = "[t/N]" if default_no else "[T/n]"
    while True:
        try:
            raw = input(f"{prompt} {suffix} ").strip().lower()
        except EOFError:
            return not default_no

        if not raw:
            return not default_no

        if raw in _YES_TOKENS:
            return True
        if raw in _NO_TOKENS:
            return False

        print(
            f"Nieprawidlowa odpowiedz: {raw!r}. Wpisz 't' (tak) albo 'n' (nie). "
            "Enter = default. Nic nie zostalo zmienione."
        )


def ask_retry() -> bool:
    """Po odmowie [n] pyta: 'sprobowac jeszcze raz wygenerowac dokumentacje?'.

    Domyslnie NIE (Enter = stop calkowity). True = ponowny call AI dla
    tego samego commita, False = konczymy bieg bez zapisu.
    """

    return ask_confirm(
        prompt="Sprobowac jeszcze raz wygenerowac dokumentacje dla tego commita?",
        default_no=True,
    )


def ask_accept_pending() -> bool:
    """Po ``apply_pending`` pyta usera o decyzje podjeta-w-Obsidianie.

    Agent zapisal dokumentacje do vaulta w **diff-view**:

    - GREEN (callout ``[!tip]+``) = NOWA WERSJA, czeka na akceptacje
    - RED   (callout ``[!failure]+``) = POPRZEDNIA WERSJA (dla ``update``)

    User otwiera Obsidiana, porownuje czerwone i zielone bloki,
    wraca do terminala i wybiera:

    - ``T`` → ``finalize_pending`` usuwa **oba** kolory, zostaje sama nowa
      tresc, agent commituje vault
    - ``n`` → ``rollback_pending`` przywraca vault dokladnie do stanu
      sprzed propozycji (bez commita)

    Enter = odmowa (bezpieczny default — vault zostaje cofniety).
    """

    return ask_confirm(
        prompt=(
            "Akceptujesz zmiany w vaulcie? "
            "[T = zostaje tylko GREEN (commit) | n = wszystko cofniete]"
        ),
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


def _action_description(write: ProposedWrite) -> str:
    lines = write.content.count("\n") + (1 if write.content and not write.content.endswith("\n") else 0)
    first_line = write.content.splitlines()[0] if write.content else ""
    if first_line.startswith("---"):
        first_line = "(frontmatter + ...)"
    return f"{lines} linii, start: {first_line[:60]!r}" if first_line else f"{lines} linii"


def _extract_pending_concept_rows(
    content: str,
) -> list[tuple[str, list[str], str | None]]:
    """Parsuje tabele ``## Placeholdery`` w ``_Pending_Concepts.md`` (Faza 6).

    Zwraca liste ``(name, sources, hint)`` per wiersz. Gdy parsing padnie
    (brak sekcji, brak tabeli, zle komorki) \u2014 zwraca ``[]``: wolajacy
    fallbackuje do pelnego podgladu tresci.

    Preview chce tej listy bez reszty kolumn \u2014 user ma zobaczyc **kogo
    zarejestrowano**, nie cala formatke GFM.
    """

    if not content:
        return []

    span = find_heading_span(content, PENDING_CONCEPTS_SECTION)
    if span is None:
        return []

    lines, _ = _split_lines_preserving(content)
    in_fence = _iter_code_fence_mask(lines)

    sep_idx: int | None = None
    i = span.body_start
    while i < span.body_end - 1:
        if in_fence[i]:
            i += 1
            continue
        if _parse_pipe_row(lines[i]) is not None and i + 1 < span.body_end:
            if _TABLE_SEPARATOR_RE.match(lines[i + 1]):
                sep_idx = i + 1
                break
        i += 1

    if sep_idx is None:
        return []

    rows: list[tuple[str, list[str], str | None]] = []
    for j in range(sep_idx + 1, span.body_end):
        if in_fence[j]:
            break
        cells = _parse_pipe_row(lines[j])
        if cells is None or not cells:
            break
        name = cells[0].replace(r"\|", "|").strip()
        if not name:
            continue
        sources_raw = cells[1].replace(r"\|", "|").strip() if len(cells) > 1 else ""
        sources = [s.strip() for s in sources_raw.split(",") if s.strip()]
        hint_raw = cells[2].replace(r"\|", "|").strip() if len(cells) > 2 else ""
        hint = hint_raw or None
        rows.append((name, sources, hint))
    return rows


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
