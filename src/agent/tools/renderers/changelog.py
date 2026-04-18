"""``render_changelog_entry`` — pojedynczy wpis changelogu (Faza 5).

Uwaga o trybie dzialania: w odroznieniu od ``render_hub`` / ``render_decision``,
changelog w vaulcie to **zbiorczy plik per dzien** (``changelog/YYYY-MM-DD.md``).
Renderer produkuje zatem NIE cala notatke z frontmatterem, tylko **fragment
body** dla pojedynczego commita — aby narzedzie ``CreateChangelogEntryTool``
moglo:

- utworzyc plik z frontmatterem + pierwszym wpisem gdy nie istnieje,
- dopisac kolejny wpis (bez frontmattera) kiedy plik juz istnieje.

Dodatkowa funkcja ``render_changelog_file`` sklada pelny plik z
frontmatterem + wpisem — uzywana gdy tworzymy swiezy plik dnia.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.agent.tools.renderers._frontmatter import build_frontmatter

__all__ = ["ChangelogEntryBullet", "render_changelog_entry", "render_changelog_file"]


@dataclass(slots=True, frozen=True)
class ChangelogEntryBullet:
    """Pojedynczy bullet w sekcji "Co sie zmienilo" wpisu changelogu.

    :ivar text: tresc (ze swoimi wikilinkami).
    :ivar impact: opcjonalny dopisek "(dotyczy: [[X]], [[Y]])".
    """

    text: str
    impact: str | None = None


def render_changelog_entry(
    *,
    commit_short_sha: str,
    commit_subject: str,
    commit_author: str,
    commit_date: str,
    what_changed: Sequence[ChangelogEntryBullet],
    context: str | None = None,
) -> str:
    """Buduje markdown **fragmentu** dla jednego commita w pliku changelogu.

    Format (sekcja trzeciego poziomu, zeby latwo dopisywac kolejne
    pod jednym dziennym headingiem ``##``):

    ::

        ### {short_sha} — {subject}

        **Autor:** {author}  \n**Data:** {date}

        **Co sie zmienilo:**

        - bullet 1
        - bullet 2

        **Kontekst:**

        dlaczego zmiana powstala (opcjonalnie).

    :param commit_short_sha: 7-znakowy SHA.
    :param commit_subject: pierwsza linia message'a commita.
    :param commit_author: autor (``Imie Nazwisko <email>``).
    :param commit_date: ``YYYY-MM-DD HH:MM`` albo sama data.
    :param what_changed: lista bulletow.
    :param context: opcjonalny paragraf "Kontekst" (2-3 zdania).
    """

    if not commit_short_sha.strip():
        raise ValueError("commit_short_sha musi byc niepusty")
    if not commit_subject.strip():
        raise ValueError("commit_subject musi byc niepusty")
    if not what_changed:
        raise ValueError("what_changed musi miec co najmniej 1 bullet")

    parts: list[str] = [
        f"### {commit_short_sha.strip()} — {commit_subject.strip()}",
        "",
        f"**Autor:** {commit_author.strip() or '_(nieznany)_'}  ",
        f"**Data:** {commit_date.strip() or '_(nieznana)_'}",
        "",
        "**Co sie zmienilo:**",
        "",
    ]
    for bullet in what_changed:
        if not isinstance(bullet, ChangelogEntryBullet):
            raise TypeError("what_changed musi byc Sequence[ChangelogEntryBullet]")
        text = bullet.text.strip()
        if not text:
            continue
        impact = (bullet.impact or "").strip()
        if impact:
            parts.append(f"- {text} _(dotyczy: {impact})_")
        else:
            parts.append(f"- {text}")
    parts.append("")

    if context and context.strip():
        parts.append("**Kontekst:**")
        parts.append("")
        parts.append(context.strip())
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def render_changelog_file(
    *,
    date: str,
    parent_moc: str,
    tags: Sequence[str] | None,
    entries: Sequence[str],
) -> str:
    """Sklada pelny plik changelogu na dany dzien z listy wpisow.

    :param date: ``YYYY-MM-DD`` — uzywane w tytule ``# Changelog {date}``
        i w polach ``created`` / ``updated`` frontmattera.
    :param parent_moc: rodzicielski MOC (zwykle ``"MOC___Changelog"``).
    :param tags: tagi; ``changelog`` dodany automatycznie.
    :param entries: lista wyrenderowanych wpisow (stringi z
        ``render_changelog_entry``). Sklejane ``\\n\\n``-ami.

    Plik zawiera sekcje dzienna ``## {date}`` i pod nia wpisy. Gdy pozniej
    dopisujemy wpis do istniejacego pliku, agent uzywa ``append_section``
    z heading = ``date`` albo ``append_to_note`` — ten renderer potrzebny
    jest tylko przy tworzeniu swiezego pliku.
    """

    fm = build_frontmatter(
        note_type="changelog",
        tags=tags,
        parent=parent_moc,
        related=None,
        status=None,
        created=date,
        updated=date,
    )

    body_parts: list[str] = [
        f"# Changelog {date}",
        "",
        f"## {date}",
        "",
    ]
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            continue
        body_parts.append(entry.rstrip())
        body_parts.append("")

    body = "\n".join(body_parts).rstrip() + "\n"
    return fm + "\n" + body
