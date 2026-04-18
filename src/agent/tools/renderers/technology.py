"""``render_technology`` — renderer notatki typu technology (Faza 5).

**Semantyka notatki typu technology (AthleteStack):**

Technology to konkretna **biblioteka / serwis / platforma** uzywana w
systemie. Przyklady: ``Qdrant``, ``PostgreSQL``, ``FastAPI``, ``Kafka``.

W odroznieniu od ``concept`` (pojecie abstrakcyjne), ``technology``
odnosi sie do **konkretnego produktu** z nazwa wlasna. Notatka sluzy
jako "karta technologii" — do czego u nas sluzy, gdzie jest zainstalowana,
jakie sa alternatywy, z czym jest sprzezona.

Stale sekcje (w kolejnosci):

1. **Rola (1 zdanie)** — prolog pod tytulem, zwiezle: "do czego tej
   technologii uzywamy". Field args: ``role``.
2. **``## Do czego uzywamy``** — pelniejszy opis (2-6 zdan, listy).
   Field args: ``used_for``.
3. **``## Alternatywy odrzucone``** — opcjonalnie (tabela, analogicznie
   jak w concept).
4. **``## Linki``** — opcjonalnie bullet list linkow (dokumentacja,
   repo, wersja).
5. **``## Powiazane notatki``** — reader of hub/related renders.

**Pole ``role`` trafia ROWNIEZ do frontmattera** (jako ``extra={"role":
role}``) — ``ConsistencyReport`` w Fazie 5 wymaga obecnosci ``role``
w frontmatterze notatki typu technology, zeby Dataview mogl wyswietlic
"karta technologii" z oneliner-opisem bez czytania body.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.agent.tools.renderers._frontmatter import build_frontmatter
from src.agent.tools.renderers.concept import ConceptAlternative, _inline_cell

__all__ = ["TechnologyLink", "render_technology"]


@dataclass(slots=True, frozen=True)
class TechnologyLink:
    """Link w sekcji "## Linki" notatki technology.

    :ivar label: tekst opisu (np. ``"Dokumentacja oficjalna"``).
    :ivar url: URL albo wikilink wewnatrz vaulta.
    """

    label: str
    url: str


def render_technology(
    *,
    title: str,
    role: str,
    used_for: str,
    parent: str | None,
    related: Sequence[str] | None,
    tags: Sequence[str] | None,
    created: str,
    updated: str | None = None,
    status: str | None = None,
    alternatives_rejected: Sequence[ConceptAlternative] | None = None,
    links: Sequence[TechnologyLink] | None = None,
) -> str:
    """Sklada pelny markdown notatki typu ``technology``.

    :param title: ``# {title}`` — nazwa technologii.
    :param role: jedno zdanie "do czego jest" (prolog + pole frontmattera).
    :param used_for: body sekcji ``## Do czego uzywamy``.
    :param parent, related, tags, created, updated, status: jak w pozostalych rendererach.
    :param alternatives_rejected: lista ``ConceptAlternative`` dla sekcji
        "## Alternatywy odrzucone". ``None`` / pusta → sekcja pominieta.
    :param links: lista ``TechnologyLink`` dla sekcji "## Linki".
    """

    if not title.strip():
        raise ValueError("title technology musi byc niepustym stringiem")
    if not role.strip():
        raise ValueError("role technology musi byc niepustym stringiem")
    if not used_for.strip():
        raise ValueError("used_for technology musi byc niepustym stringiem")

    fm = build_frontmatter(
        note_type="technology",
        tags=tags,
        parent=parent,
        related=related,
        status=status,
        created=created,
        updated=updated,
        extra={"role": role.strip()},
    )

    parts: list[str] = [
        f"# {title.strip()}",
        "",
        role.strip(),
        "",
        "## Do czego uzywamy",
        "",
        used_for.strip(),
        "",
    ]

    if alternatives_rejected:
        parts.append("## Alternatywy odrzucone")
        parts.append("")
        parts.append("| Alternatywa | Dlaczego odrzucona |")
        parts.append("|---|---|")
        for alt in alternatives_rejected:
            if not isinstance(alt, ConceptAlternative):
                raise TypeError(
                    "alternatives_rejected musi byc Sequence[ConceptAlternative]"
                )
            parts.append(f"| {_inline_cell(alt.name)} | {_inline_cell(alt.reason)} |")
        parts.append("")

    if links:
        parts.append("## Linki")
        parts.append("")
        for link in links:
            if not isinstance(link, TechnologyLink):
                raise TypeError("links musi byc Sequence[TechnologyLink]")
            label = link.label.strip()
            url = link.url.strip()
            if not label or not url:
                continue
            parts.append(f"- [{label}]({url})") if not url.startswith("[[") else parts.append(
                f"- {url} — {label}"
            )
        parts.append("")

    body = "\n".join(parts).rstrip() + "\n"
    return fm + "\n" + body
