"""``render_concept`` — renderer notatki typu concept (Faza 5).

**Semantyka notatki typu concept (AthleteStack):**

Concept to **pojecie** — kluczowy termin, paradygmat, wzorzec. Nie jest
to modul kodu ani decyzja; to raczej slowniczek z rozszerzonym kontekstem.
Przyklady: ``Modularny_monolit``, ``Event_sourcing``, ``Vector_embeddings``.

Stale sekcje (w kolejnosci):

1. **Definicja** — 1-3 zdania "co to jest" (bez headingu, tuz pod tytulem).
2. **``## Kontekst``** — gdzie/kiedy to stosujemy w naszym projekcie,
   czemu to pojecie jest istotne dla tego systemu. 2-6 zdan.
3. **``## Alternatywy odrzucone``** — opcjonalnie, gdy pojecie jest wyborem
   kosztem innych (np. concept ``Modularny_monolit`` odrzuca
   ``Mikroserwisy``). Pusta lista = pomijamy sekcje.
4. **``## Powiazane notatki``** — opcjonalnie, bullet list wikilinkow.

Pole ``alternatives`` (args) to lista ``(name, reason)`` — renderer
wyrenderuje jako tabele ``| Alternatywa | Dlaczego odrzucona |``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.agent.tools.renderers._frontmatter import build_frontmatter

__all__ = ["ConceptAlternative", "render_concept"]


@dataclass(slots=True, frozen=True)
class ConceptAlternative:
    """Alternatywa odrzucona na rzecz tego konceptu.

    :ivar name: nazwa alternatywy (tekst albo wikilink).
    :ivar reason: jedno-dwuzdaniowe uzasadnienie odrzucenia.
    """

    name: str
    reason: str


def render_concept(
    *,
    title: str,
    definition: str,
    context: str,
    parent: str | None,
    related: Sequence[str] | None,
    tags: Sequence[str] | None,
    created: str,
    updated: str | None = None,
    status: str | None = None,
    alternatives: Sequence[ConceptAlternative] | None = None,
) -> str:
    """Sklada pelny markdown notatki typu ``concept``.

    :param title: tytul ``# {title}``.
    :param definition: 1-3 zdania definicji (prolog, bez headingu).
    :param context: body sekcji ``## Kontekst``.
    :param parent: wikilink do rodzica (zwykle MOC albo hub).
    :param related: lista wikilinkow do ``related`` we frontmatterze.
    :param tags: tagi; ``concept`` dodany automatycznie.
    :param created, updated, status: jak w ``build_frontmatter``.
    :param alternatives: lista ``ConceptAlternative``. ``None`` / pusta
        → sekcja "## Alternatywy odrzucone" pominieta.
    """

    if not title.strip():
        raise ValueError("title concept musi byc niepustym stringiem")
    if not definition.strip():
        raise ValueError("definition concept musi byc niepustym stringiem")
    if not context.strip():
        raise ValueError("context concept musi byc niepustym stringiem")

    fm = build_frontmatter(
        note_type="concept",
        tags=tags,
        parent=parent,
        related=related,
        status=status,
        created=created,
        updated=updated,
    )

    parts: list[str] = [
        f"# {title.strip()}",
        "",
        definition.strip(),
        "",
        "## Kontekst",
        "",
        context.strip(),
        "",
    ]

    if alternatives:
        parts.append("## Alternatywy odrzucone")
        parts.append("")
        parts.append("| Alternatywa | Dlaczego odrzucona |")
        parts.append("|---|---|")
        for alt in alternatives:
            if not isinstance(alt, ConceptAlternative):
                raise TypeError(
                    "alternatives musi byc Sequence[ConceptAlternative], dostalismy "
                    f"{type(alt).__name__}"
                )
            name = _inline_cell(alt.name)
            reason = _inline_cell(alt.reason)
            parts.append(f"| {name} | {reason} |")
        parts.append("")

    body = "\n".join(parts).rstrip() + "\n"
    return fm + "\n" + body


def _inline_cell(value: str) -> str:
    """Escape ``|`` i spakowanie wieloliniowego tekstu w jedna linie tabeli."""

    flat = " ".join(value.strip().splitlines())
    return flat.replace("|", r"\|")
