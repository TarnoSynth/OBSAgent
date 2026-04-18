"""``render_decision`` — renderer notatki typu decision (Faza 5).

**Semantyka notatki typu decision (AthleteStack):**

Decision to **ADR w stylu AthleteStack** — decyzja architektoniczna ze
strukturowana sekcja konsekwencji, planem migracji i linkami do
powiazanych konceptow / technologii. Przyklady:
``UseQdrantOverPgvector``, ``ModularMonolithOverMicroservices``,
``AsyncEverywhere``.

Stale sekcje (w kolejnosci):

1. **Krotkie streszczenie** — 1-2 zdania "co zdecydowano" (prolog).
2. **``## Kontekst``** — sytuacja / ograniczenia / alternatywy rozwazane.
3. **``## Decyzja``** — konkretne "uzywamy X poniewaz ...".
4. **``## Uzasadnienie``** — rozszerzenie decyzji (dlaczego X a nie Y, Z).
5. **``## Konsekwencje``** — sztywna podsekcja:
   - **Pozytywne** — lista bulletow.
   - **Negatywne** — lista bulletow.
6. **``## Migracja``** — opcjonalnie: co trzeba zrobic w kodzie /
   infrastrukturze. Pominiete gdy decyzja dotyczy czegos nowego (brak
   migracji).
7. **``## Powiazane``** — bullet list wikilinkow.

Renderer **nie** dopisuje sam decyzji do tabeli huba. To robi
``CreateDecisionTool`` — wola ``add_table_row`` jako osobny krok (zgodnie
z sugestia Pytania Otwartego #1 w planie: automatycznie wpiac w tabele
"Decyzje architektoniczne" rodzica, bo to idiom AthleteStack).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.agent.tools.renderers._frontmatter import build_frontmatter

__all__ = ["DecisionConsequences", "render_decision"]


@dataclass(slots=True, frozen=True)
class DecisionConsequences:
    """Struktura konsekwencji decyzji (pozytywne + negatywne).

    :ivar positive: lista 1-zdaniowych plusow. Moze byc pusta.
    :ivar negative: lista 1-zdaniowych minusow. Moze byc pusta.

    Puste obie listy sa dopuszczalne (niektore decyzje sa "czyste"), ale
    zwykle co najmniej jedna strona ma wpisy — jesli obie puste, to model
    prawdopodobnie nie przemyslal do konca i warto ostrzec. Renderer
    **nie waliduje** "co najmniej jeden wpis" — to domena narzedzia
    ``CreateDecisionTool``, ktore moze dodac ``ToolResult(ok=False, ...)``.
    """

    positive: Sequence[str]
    negative: Sequence[str]


def render_decision(
    *,
    title: str,
    summary: str,
    context: str,
    decision: str,
    rationale: str,
    consequences: DecisionConsequences,
    parent: str | None,
    related: Sequence[str] | None,
    tags: Sequence[str] | None,
    created: str,
    updated: str | None = None,
    status: str | None = None,
    migration: str | None = None,
) -> str:
    """Sklada pelny markdown notatki typu ``decision``.

    :param title: ``# ADR — {title}``. Celowe "ADR —" w headingu —
        konwencja AthleteStack, zeby rozpoznac ADR-y wzrokowo w Obsidianie.
    :param summary: 1-2 zdania prologu. Bez headingu.
    :param context: body sekcji ``## Kontekst``.
    :param decision: body sekcji ``## Decyzja``.
    :param rationale: body sekcji ``## Uzasadnienie``.
    :param consequences: struktura DecisionConsequences.
    :param migration: opcjonalny body sekcji ``## Migracja``. ``None`` lub
        pusty string → sekcja pominieta.
    :param parent, related, tags, created, updated, status: jak w pozostalych rendererach.
    """

    if not title.strip():
        raise ValueError("title decision musi byc niepustym stringiem")
    if not summary.strip():
        raise ValueError("summary decision musi byc niepustym stringiem")
    if not context.strip():
        raise ValueError("context decision musi byc niepustym stringiem")
    if not decision.strip():
        raise ValueError("decision (body) decision musi byc niepustym stringiem")
    if not rationale.strip():
        raise ValueError("rationale decision musi byc niepustym stringiem")
    if not isinstance(consequences, DecisionConsequences):
        raise TypeError(
            f"consequences musi byc DecisionConsequences, dostalismy {type(consequences).__name__}"
        )

    fm = build_frontmatter(
        note_type="decision",
        tags=tags,
        parent=parent,
        related=related,
        status=status,
        created=created,
        updated=updated,
    )

    parts: list[str] = [
        f"# ADR — {title.strip()}",
        "",
        summary.strip(),
        "",
        "## Kontekst",
        "",
        context.strip(),
        "",
        "## Decyzja",
        "",
        decision.strip(),
        "",
        "## Uzasadnienie",
        "",
        rationale.strip(),
        "",
        "## Konsekwencje",
        "",
        "**Pozytywne:**",
        "",
    ]
    parts.extend(_bullet_list(consequences.positive, empty_text="_(brak jawnie wymienionych)_"))
    parts.append("")
    parts.append("**Negatywne:**")
    parts.append("")
    parts.extend(_bullet_list(consequences.negative, empty_text="_(brak jawnie wymienionych)_"))
    parts.append("")

    if migration and migration.strip():
        parts.append("## Migracja")
        parts.append("")
        parts.append(migration.strip())
        parts.append("")

    body = "\n".join(parts).rstrip() + "\n"
    return fm + "\n" + body


def _bullet_list(items: Sequence[str], *, empty_text: str) -> list[str]:
    """Zamienia liste stringow na linie bulletow; pusta → 1 wpis ``empty_text``."""

    real = [i.strip() for i in items if isinstance(i, str) and i.strip()]
    if not real:
        return [empty_text]
    return [f"- {item}" for item in real]
