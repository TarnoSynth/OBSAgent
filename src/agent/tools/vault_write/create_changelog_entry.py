"""``create_changelog_entry`` — wpis changelogu per commit (Faza 5 refaktoru).

Changelog w stylu AthleteStack to **zbiorczy plik per dzien**
(``changelog/YYYY-MM-DD.md``). Pojedynczy commit = jedna sekcja ``###``
w tym pliku. Dlatego to narzedzie ma dwa tryby:

1. **Swiezy dzien** — plik nie istnieje → ``create`` z pelnym
   frontmatterem + ``## {date}`` + pierwszym wpisem.
2. **Istniejacy dzien** — plik juz jest → ``append`` dopisuje kolejny
   ``### {sha} — {subject}`` pod nagrowkiem ``## {date}``. Jezeli plik
   jest ale brakuje sekcji dziennej → dopisujemy pod pliku heading.

Koalescencja z Fazy 3 (``register_granular_update``) zlewa wielokrotne
wywolania tego narzedzia na te sama sciezke w jeden ``update``, wiec
model moze zalogowac 3 commity pod rzad bez strachu o 3 osobne akcje.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext
from src.agent.tools.renderers.changelog import (
    ChangelogEntryBullet,
    render_changelog_entry,
    render_changelog_file,
)
from src.agent.tools.vault_write._common import (
    build_and_register_action,
    normalize_path_or_error,
)
from src.agent.tools.vault_write._granular import (
    compute_effective_content,
    register_granular_update,
)
from src.agent.tools.vault_write._markdown_ops import (
    MarkdownOpsError,
    find_heading_span,
)

__all__ = ["CreateChangelogEntryTool"]


class _BulletArg(BaseModel):
    """Pojedynczy bullet w sekcji 'Co sie zmienilo'."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        ...,
        min_length=1,
        description=(
            "Tresc bulletu — co dokladnie sie zmienilo z perspektywy projektu. "
            "1 zdanie. Uzywaj wikilinkow do zmienionych modulow ``[[Auth]]``."
        ),
    )
    impact: str | None = Field(
        default=None,
        description=(
            "Opcjonalna lista modulow/obszarow na ktore ta zmiana wplywa. "
            "Format: 'wikilink, wikilink' albo 'Auth module' — renderer owinie "
            "to w ``_(dotyczy: ...)_``."
        ),
    )


class _CreateChangelogEntryArgs(BaseModel):
    """Schemat argumentow ``create_changelog_entry``.

    Model przekazuje ``date`` separate od ``path`` — narzedzie buduje
    ``path = f"changelog/{date}.md"``. Minimalizuje to ryzyko pomylki
    w sciezce (data w pliku NIE zgadza sie z naglowkiem).
    """

    model_config = ConfigDict(extra="forbid")

    date: str = Field(
        ...,
        min_length=10,
        max_length=10,
        description=(
            "Data commita w formacie ``YYYY-MM-DD``. To sama data sluzy jako "
            "nazwa pliku (``changelog/YYYY-MM-DD.md``) i heading ``## date``."
        ),
    )
    commit_short_sha: str = Field(
        ...,
        min_length=4,
        max_length=40,
        description="Short SHA commita (7-10 znakow), np. ``a1b2c3d``.",
    )
    commit_subject: str = Field(
        ...,
        min_length=1,
        description=(
            "Pierwsza linia commit message'a. Bedzie heading ``### sha — subject``."
        ),
    )
    commit_author: str = Field(
        ...,
        min_length=1,
        description="Autor commita, format ``Imie Nazwisko <email>`` albo samo imie.",
    )
    commit_date: str = Field(
        ...,
        min_length=10,
        description=(
            "Pelna data/czas commita (``YYYY-MM-DD HH:MM`` lub sama data). "
            "Wyswietlana pod naglowkiem '### ...'."
        ),
    )
    what_changed: list[_BulletArg] = Field(
        ...,
        min_length=1,
        description=(
            "Lista bulletow 'Co sie zmienilo'. Konkretnie, z perspektywy usera/"
            "projektu — nie per-plik, nie per-klasa. 2-5 punktow."
        ),
    )
    context: str | None = Field(
        default=None,
        description=(
            "Opcjonalny paragraf 'Kontekst' — dlaczego zmiana powstala, co bylo "
            "przed. 2-3 zdania. Pominac dla trywialnych commitow."
        ),
    )
    parent_moc: str = Field(
        default="MOC___Changelog",
        description=(
            "Rodzicielski MOC dla changelogu — uzywany TYLKO gdy tworzymy "
            "swiezy plik dnia (pierwszy commit danego dnia). Default: "
            "``MOC___Changelog``."
        ),
    )
    tags: list[str] | None = Field(
        default=None,
        description=(
            "Dodatkowe tagi dla frontmattera — tylko gdy tworzymy swiezy plik. "
            "Tag ``changelog`` jest dodawany automatycznie."
        ),
    )


class CreateChangelogEntryTool(Tool):
    """Dodaje wpis changelogu per commit do ``changelog/YYYY-MM-DD.md``.

    - Nie istnieje → ``create`` z pelnym plikiem dnia.
    - Istnieje → ``append``/``update`` dopisujacy sekcje ``###`` pod
      istniejacym headingiem ``## {date}``.
    """

    name = "create_changelog_entry"
    description = (
        "Dodaje pojedynczy wpis changelogu (per commit) do pliku "
        "'changelog/YYYY-MM-DD.md'. Automatycznie tworzy plik dnia gdy "
        "nie istnieje; w przeciwnym razie dopisuje kolejna sekcje '###' "
        "pod naglowkiem '## {date}'. Finalizacja w submit_plan."
    )

    def input_schema(self) -> dict[str, Any]:
        return _CreateChangelogEntryArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _CreateChangelogEntryArgs.model_validate(args)
        except ValidationError as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        date = parsed.date.strip()
        path = f"changelog/{date}.md"
        normalized_path = normalize_path_or_error(path)
        if isinstance(normalized_path, ToolResult):
            return normalized_path

        bullets = [
            ChangelogEntryBullet(text=b.text, impact=b.impact) for b in parsed.what_changed
        ]

        entry = render_changelog_entry(
            commit_short_sha=parsed.commit_short_sha,
            commit_subject=parsed.commit_subject,
            commit_author=parsed.commit_author,
            commit_date=parsed.commit_date,
            what_changed=bullets,
            context=parsed.context,
        )

        effective = compute_effective_content(ctx, normalized_path)

        if effective is None:
            full_file = render_changelog_file(
                date=date,
                parent_moc=parsed.parent_moc,
                tags=parsed.tags,
                entries=[entry],
            )
            result = build_and_register_action(
                ctx=ctx,
                tool_name=self.name,
                action_type="create",
                normalized_path=normalized_path,
                content=full_file,
            )
            if result.ok:
                result = ToolResult(
                    ok=True,
                    content=(
                        f"{result.content}\n"
                        f"CHANGELOG created: date={date}, sha={parsed.commit_short_sha}, "
                        "first entry of the day."
                    ),
                )
            return result

        try:
            day_span = find_heading_span(effective, date)
        except MarkdownOpsError as exc:
            return ToolResult(
                ok=False,
                error=f"Nieparsowalny istniejacy plik changelogu {path!r}: {exc}",
            )

        if day_span is None:
            new_content = _append_day_section(effective, date, entry)
            op_summary = f"append_day_section('{date}') + entry ({parsed.commit_short_sha})"
        else:
            new_content = _append_entry_within_day(effective, day_span, entry)
            op_summary = f"append_entry({parsed.commit_short_sha}) under '## {date}'"

        return register_granular_update(
            ctx=ctx,
            tool_name=self.name,
            normalized_path=normalized_path,
            new_content=new_content,
            op_summary=op_summary,
            extra_log_args={
                "date": date,
                "commit_short_sha": parsed.commit_short_sha,
            },
        )


def _append_day_section(content: str, date: str, entry: str) -> str:
    """Dopisuje na koncu pliku ``## {date}\\n\\n{entry}``.

    Uzywane gdy plik istnieje, ale brakuje naglowka dla tego dnia (rzadki
    przypadek: user recznie zainicjowal plik, albo plik pokrywa wiele dni).
    """

    base = content if content.endswith("\n") else content + "\n"
    if not base.endswith("\n\n"):
        base += "\n"
    return base + f"## {date}\n\n" + entry.rstrip() + "\n"


def _append_entry_within_day(content: str, day_span: Any, entry: str) -> str:
    """Wkleja ``entry`` zaraz przed konca sekcji dnia (przed kolejnym ``##``)."""

    lines = content.split("\n")
    trailing_newline = content.endswith("\n")
    if trailing_newline:
        lines = lines[:-1]

    entry_text = entry.rstrip()
    entry_lines = entry_text.split("\n")

    insert_at = day_span.body_end

    if insert_at > 0 and insert_at <= len(lines):
        prev_nonempty = insert_at - 1
        while prev_nonempty >= day_span.body_start and lines[prev_nonempty] == "":
            prev_nonempty -= 1
        need_blank_before = prev_nonempty >= day_span.body_start
    else:
        need_blank_before = False

    block: list[str] = []
    if need_blank_before:
        block.append("")
    block.extend(entry_lines)
    block.append("")

    new_lines = lines[:insert_at] + block + lines[insert_at:]
    rejoined = "\n".join(new_lines)
    if trailing_newline and not rejoined.endswith("\n"):
        rejoined += "\n"
    return rejoined
