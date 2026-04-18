"""Loader szablonow notatek z folderu ``templates/``.

Szablony (`changelog`, `adr`, `module`, `doc`) to pliki `.md` z
placeholderami ``{{title}}``, ``{{date}}``, ``{{commit_short_sha}}``,
``{{commit_subject}}``, ``{{commit_author}}``, ``{{commit_date}}``.

Ta warstwa **nie wypelnia** placeholderow \u2014 zwraca raw tresc szablonu.
AI dostaje szablony w user promcie jako referencje struktury i sam
sklada koncowa notatke. Wypelnianie placeholderow byloby niepotrzebnym
ograniczeniem (AI i tak musi dopasowac tresc do commita).

Mimo to wystawiamy pomocnicze ``render_template`` dla testow i
ewentualnego debugowania.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

TemplateName = Literal["changelog", "adr", "module", "doc"]
ExampleName = Literal["hub", "concept", "technology", "decision", "module"]

TEMPLATES_DIR_NAME = "templates"
EXAMPLES_DIR_NAME = "examples"
KNOWN_TEMPLATES: tuple[TemplateName, ...] = ("changelog", "adr", "module", "doc")
KNOWN_EXAMPLES: tuple[ExampleName, ...] = (
    "hub",
    "concept",
    "technology",
    "decision",
    "module",
)


def _default_templates_dir() -> Path:
    """Katalog `templates/` na poziomie repo (nad `src/`)."""

    return Path(__file__).resolve().parents[2] / TEMPLATES_DIR_NAME


def _default_examples_dir() -> Path:
    """Katalog `templates/examples/` - few-shot dla Fazy 5 refaktoru."""

    return _default_templates_dir() / EXAMPLES_DIR_NAME


def load_template(name: TemplateName, *, templates_dir: Path | None = None) -> str:
    """Wczytuje surowy tekst szablonu `name` z ``templates/<name>.md``.

    Zwraca raw content z placeholderami. Agent prompt builder wstawi go
    jako "referencja struktury" w user promcie.
    """

    if name not in KNOWN_TEMPLATES:
        raise ValueError(
            f"Nieznany szablon: {name!r}. Dozwolone: {', '.join(KNOWN_TEMPLATES)}."
        )

    base_dir = templates_dir or _default_templates_dir()
    path = base_dir / f"{name}.md"
    if not path.is_file():
        raise ValueError(f"Brak pliku szablonu: {path}")
    return path.read_text(encoding="utf-8")


def load_all_templates(*, templates_dir: Path | None = None) -> dict[str, str]:
    """Wczytuje wszystkie znane szablony jako dict ``{name: raw_content}``."""

    return {name: load_template(name, templates_dir=templates_dir) for name in KNOWN_TEMPLATES}


def load_example(name: ExampleName, *, examples_dir: Path | None = None) -> str:
    """Wczytuje przyklad notatki AthleteStack-style z ``templates/examples/``.

    Uzywane w Fazie 5 refaktoru jako **few-shot** w system prompcie — modelo
    widzi pelen plik z frontmatterem + struktura sekcji, wiec uczy sie
    lokalnej typologii notatek (hub, concept, technology, decision, module).
    """

    if name not in KNOWN_EXAMPLES:
        raise ValueError(
            f"Nieznany przyklad: {name!r}. Dozwolone: {', '.join(KNOWN_EXAMPLES)}."
        )

    base_dir = examples_dir or _default_examples_dir()
    path = base_dir / f"{name}.md"
    if not path.is_file():
        raise ValueError(f"Brak pliku przykladu: {path}")
    return path.read_text(encoding="utf-8")


def load_all_examples(*, examples_dir: Path | None = None) -> dict[str, str]:
    """Wczytuje wszystkie przyklady AthleteStack-style jako ``{name: raw_content}``.

    Wynik ma byc **stabilny** miedzy uruchomieniami (taka sama kolejnosc,
    ta sama tresc) zeby prompt caching Anthropica widzial niezmienny blok
    system promptu.
    """

    return {
        name: load_example(name, examples_dir=examples_dir)
        for name in KNOWN_EXAMPLES
    }


def render_template(name: TemplateName, context: dict[str, str], *, templates_dir: Path | None = None) -> str:
    """Renderuje szablon wstawiajac ``{{key}}`` z ``context``.

    Pomocnicza funkcja \u2014 w zwyklym przeplywie agent **nie** renderuje
    szablonow po swojej stronie (to robi AI generujac tresc). Sluzy
    testom, devtoolsom, podgladowi "jak wyglada szablon wypelniony".
    Brakujace klucze zostawiane sa nietkniete (nie rzucamy wyjatkow).
    """

    raw = load_template(name, templates_dir=templates_dir)
    for key, value in context.items():
        raw = raw.replace("{{" + key + "}}", str(value))
    return raw
