"""Renderery domenowe dla notatek w stylu AthleteStack (Faza 5 refaktoru).

**Cel warstwy:**

Model LLM w Fazie 5 przestaje produkowac gole markdowny dla notatek typu
``hub`` / ``concept`` / ``technology`` / ``decision`` / ``module`` /
``changelog``. Zamiast tego wypelnia **strukturowane pola** (``ArgsModel``
per narzedzie), a renderery deterministycznie sklejaja je w spojny
markdown zgodny z konwencja AthleteStack (gesty graf wikilinkow, sekcje
"Dlaczego", tabele decyzji).

**Zasady rendererow:**

- Czyste funkcje ``render_*(args: ArgsModel) -> str`` — bez I/O, bez
  side-effectow, bez dotykania ``ToolExecutionContext``.
- Frontmatter zawsze przez ``build_frontmatter`` — jedno zrodlo prawdy
  dla YAML headera (typy pol, kolejnosc kluczy, format dat).
- Body strukturowane w przewidywalne sekcje (``##`` drugiego poziomu).
  Kolejnosc i tytuly sekcji sa **staly per typ** — dzieki temu modelowi
  latwo sie uczyc typologii, a zewnetrzne narzedzia (Dataview, skrypty)
  moga polegac na strukturze.
- Brakujace pola opcjonalne sa pomijane w wyjsciu (nie renderujemy
  pustych sekcji "Alternatywy odrzucone: _(brak)_" — zasmieca body).

**Eksporty publiczne:**

- ``build_frontmatter`` — buduje YAML frontmatter (jedno zrodlo prawdy).
- ``render_hub`` — renderer dla ``type: hub``.
- ``render_concept`` — dla ``type: concept``.
- ``render_technology`` — dla ``type: technology``.
- ``render_decision`` — dla ``type: decision`` (ADR w stylu AthleteStack).
- ``render_module`` — dla ``type: module``.
- ``render_changelog_entry`` — pojedynczy wpis changelogu.

Unit-testy per renderer: snapshot (golden-file) test porownujacy
wyrenderowany markdown z ``tests/fixtures/renderers/<type>_golden.md``.
"""

from src.agent.tools.renderers._frontmatter import build_frontmatter
from src.agent.tools.renderers.changelog import render_changelog_entry
from src.agent.tools.renderers.concept import render_concept
from src.agent.tools.renderers.decision import render_decision
from src.agent.tools.renderers.hub import render_hub
from src.agent.tools.renderers.module import render_module
from src.agent.tools.renderers.technology import render_technology

__all__ = [
    "build_frontmatter",
    "render_changelog_entry",
    "render_concept",
    "render_decision",
    "render_hub",
    "render_module",
    "render_technology",
]
