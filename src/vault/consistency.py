"""Analiza spojnosci grafu vaulta (Faza 4c).

**Czysta funkcja** ``analyze(knowledge) -> ConsistencyReport`` — bez side-effects,
bez zapisu, bez I/O. Operuje wylacznie na zbudowanym ``VaultKnowledge``
(snapshot vaulta po ``VaultManager.scan_all``).

Decyzje "co naprawic" naleza do agenta (Faza 6 / 8). Ten modul zwraca jedynie
**raport**: co jest osierocone, gdzie sa martwe linki, ktorych modulow brakuje
w MOC-ach, ktorych tagow brakuje wzgledem typu.

Uzywany m.in. przez:

- agenta na koncu kazdego biegu (Faza 8)
- CLI `python main.py check` (Faza 9)
- ewentualne narzedzia developerskie do weryfikacji vaulta
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from src.vault.models import VaultKnowledge, VaultNote


class TagInconsistency(BaseModel):
    """Notatka o danym ``type`` ktora nie ma odpowiadajacego tagu.

    Konwencja: notatka typu ``module`` powinna miec tag ``module``,
    ``ADR`` → ``adr`` itd. Pomaga coding assistantowi krzyzowo wyszukiwac
    po obu osiach (po type i po tag).
    """

    note_path: str
    note_type: str
    expected_tag: str


class StructuralViolation(BaseModel):
    """Twarda reguła strukturalna notatki ktora zostala zlamana.

    Od Fazy 5 refaktoru (styl AthleteStack) kazdy typ notatki ma
    wlasny kontrakt — nie sprowadza sie juz do "mam tag = mam typ".
    Przyklady reguły:

    - ``type: hub`` MUSI miec tag ``hub`` we frontmatterze.
    - ``type: decision`` MUSI miec ``parent`` (wskazanie huba do
      ktorego nalezy ADR).
    - ``type: technology`` MUSI miec pole ``role`` we frontmatterze
      (krotki opis roli technologii w systemie).

    ``rule`` to maszyno-czytelny identyfikator (np. ``"decision_requires_parent"``),
    ``message`` to human-readable opis co dokladnie brakuje.
    """

    note_path: str
    note_type: str
    rule: str
    message: str


class ConsistencyReport(BaseModel):
    """Raport spojnosci grafu vaulta.

    Wszystkie pola to listy niemodyfikowalne z punktu widzenia warstwy vault —
    to **diagnostyka**. Naprawa nalezy do agenta lub usera.
    """

    total_notes: int = 0

    orphaned_notes: list[str] = Field(default_factory=list)
    """Notatki bez wchodzacych krawedzi w grafie:

    - nie sa MOC-iem ani ``_index``,
    - nikt nie linkuje do nich przez wikilink,
    - nikt nie ma ich w ``parent`` ani ``related``.

    Krotko: pliki ktorych coding assistant nie dotrze przez nawigacje.
    """

    dead_links: list[str] = Field(default_factory=list)
    """Wikilinki ``[[X]]`` w tresciach notatek dla ktorych ``X`` nie istnieje.

    Wprost przepisane z ``VaultKnowledge.orphaned_links``.
    """

    missing_in_moc: list[str] = Field(default_factory=list)
    """Notatki typu ``module`` / ``ADR`` ktore nie maja parenta wskazujacego
    na zaden istniejacy MOC i ktore nie sa zalinkowane z zadnego MOC-a.
    """

    inconsistent_tags: list[TagInconsistency] = Field(default_factory=list)
    """Notatka o danym ``type`` ktora nie ma tagu o tej samej nazwie."""

    structural_violations: list[StructuralViolation] = Field(default_factory=list)
    """Twarde naruszenia kontraktow per-typ (Faza 5 refaktoru).

    W odroznieniu od ``inconsistent_tags`` (gdzie brak tagu to drobny
    higieniczny problem) — tu lecą **blędy strukturalne**: notatka
    istnieje ale nie spelnia kontraktu swojego typu (np. ``decision``
    bez ``parent``, ``technology`` bez pola ``role``).
    """

    @property
    def total_issues(self) -> int:
        """Suma wszystkich problemow we wszystkich kategoriach."""

        return (
            len(self.orphaned_notes)
            + len(self.dead_links)
            + len(self.missing_in_moc)
            + len(self.inconsistent_tags)
            + len(self.structural_violations)
        )

    @property
    def is_clean(self) -> bool:
        """``True`` jesli vault nie ma zadnych wykrytych problemow."""

        return self.total_issues == 0


#: Zbior **kanonicznych** typow notatek rozpoznawanych przez warstwe vault.
#: Trzyma oryginalna kapitalizacje \u2014 modul/ADR/MOC sa takie same jak w
#: frontmatterze zapisywanym do pliku. Uzywany:
#:
#: - przez walidatory/narzedzia ``create_*`` (Faza 4) jako whitelist,
#: - przez ``_find_inconsistent_tags`` (ponizej) posrednio \u2014 ale to
#:   sprawdzanie iteruje po wszystkich ``note.type``, wiec nowe typy
#:   sa automatycznie objete reguly ``tag == type.lower()``.
#:
#: Faza 0 refaktoru agentic tool loop dodaje: ``hub``, ``concept``,
#: ``technology``, ``decision`` \u2014 styl AthleteStack.
KNOWN_TYPES: frozenset[str] = frozenset(
    {
        "ADR",
        "MOC",
        "changelog",
        "concept",
        "decision",
        "doc",
        "hub",
        "module",
        "technology",
    }
)


def is_known_type(note_type: str | None) -> bool:
    """Czy podany ``type`` jest jedna z kanonicznych wartosci.

    Case-sensitive \u2014 ``"ADR"`` jest znane, ``"adr"`` nie (bo w kanonicznej
    formie piszemy ADR wielkimi literami). Caller moze znormalizowac
    wartosc przed sprawdzeniem, jesli potrzebuje.

    Uzywane przez warstwe narzedzi (Faza 4) do walidacji przed zapisem.
    Tutaj wystawione, zeby jedna lista typow byla zrodlem prawdy dla
    calego projektu.
    """

    if note_type is None:
        return False
    return note_type in KNOWN_TYPES


#: Typy notatek, ktore MUSZA miec parent wskazujacy na MOC (wykryje brak
#: jako ``missing_in_moc`` w ``ConsistencyReport``). ``changelog`` jest tu
#: swiadomie \u2014 kazdy wpis changeloga ma MOC nadrzedny (np. ``MOC___Changelog``
#: jesli istnieje). Nowe typy ``hub``, ``concept``, ``technology``, ``decision``
#: sa dodane od Fazy 0 refaktoru (styl AthleteStack: kazda z nich zyje pod
#: jakims hubem / MOC-iem).
_TYPES_REQUIRING_MOC = {
    "module",
    "adr",
    "doc",
    "changelog",
    "hub",
    "concept",
    "technology",
    "decision",
}
_INDEX_FILENAMES = {"_index.md"}


def analyze(knowledge: VaultKnowledge) -> ConsistencyReport:
    """Buduje ``ConsistencyReport`` ze stanu ``VaultKnowledge`` — pure function.

    Nic nie zapisuje, nie loguje, nie modyfikuje. Bezpieczne do uzycia w petli
    sprawdzajacej.
    """

    notes_by_path = knowledge.by_path
    moc_paths = {moc.path for moc in knowledge.mocs()}
    moc_stems = {Path(p).stem for p in moc_paths}

    incoming_link_targets = _collect_incoming_link_targets(knowledge)

    orphaned_notes = _find_orphaned_notes(
        notes_by_path,
        moc_paths=moc_paths,
        incoming_targets=incoming_link_targets,
    )

    dead_links = list(knowledge.orphaned_links)

    missing_in_moc = _find_missing_in_moc(
        notes_by_path,
        moc_paths=moc_paths,
        moc_stems=moc_stems,
    )

    inconsistent_tags = _find_inconsistent_tags(notes_by_path)
    structural_violations = _find_structural_violations(notes_by_path)

    return ConsistencyReport(
        total_notes=knowledge.total_notes,
        orphaned_notes=orphaned_notes,
        dead_links=dead_links,
        missing_in_moc=missing_in_moc,
        inconsistent_tags=inconsistent_tags,
        structural_violations=structural_violations,
    )


def _collect_incoming_link_targets(knowledge: VaultKnowledge) -> set[str]:
    """Zbiera wszystkie celow do ktorych cokolwiek prowadzi (wikilink/parent/related).

    Wynik: zbior **stemow** notatek, ktore maja jakiekolwiek wchodzace polaczenie.
    """

    targets: set[str] = set()
    targets.update(knowledge.backlinks_index.keys())
    targets.update(knowledge.children_index.keys())
    targets.update(knowledge.related_index.keys())
    return targets


def _find_orphaned_notes(
    notes_by_path: dict[str, VaultNote],
    *,
    moc_paths: set[str],
    incoming_targets: set[str],
) -> list[str]:
    orphans: list[str] = []
    for path, note in notes_by_path.items():
        if path in moc_paths:
            continue
        if Path(path).name in _INDEX_FILENAMES:
            continue

        stem = Path(path).stem
        if stem in incoming_targets:
            continue
        if path in incoming_targets:
            continue

        orphans.append(path)

    orphans.sort()
    return orphans


def _find_missing_in_moc(
    notes_by_path: dict[str, VaultNote],
    *,
    moc_paths: set[str],
    moc_stems: set[str],
) -> list[str]:
    missing: list[str] = []
    for path, note in notes_by_path.items():
        if path in moc_paths:
            continue
        if (note.type or "").lower() not in _TYPES_REQUIRING_MOC:
            continue

        parent_points_to_moc = bool(note.parent) and (
            note.parent in moc_stems
            or any(Path(p).stem == note.parent for p in moc_paths)
        )
        if parent_points_to_moc:
            continue

        missing.append(path)

    missing.sort()
    return missing


def _find_structural_violations(
    notes_by_path: dict[str, VaultNote],
) -> list[StructuralViolation]:
    """Szuka twardych naruszen kontraktow per-typ (Faza 5 refaktoru).

    Obowiązujące reguły:

    - ``type: hub`` → wymaga tagu ``hub`` we frontmatterze. (Generic
      ``_find_inconsistent_tags`` tez to wylapie, ale tutaj meldujemy
      to z pełnym kontekstem jako naruszenie **strukturalne**, bo hub
      bez wlasnego tagu lamie cala nawigacje po nodach tematycznych.)
    - ``type: decision`` → wymaga pola ``parent`` (ADR zawsze wisi
      pod jakims hubem; bez parenta jest osierocony w grafie decyzji).
    - ``type: technology`` → wymaga pola ``role`` we frontmatterze
      (krotki opis roli — kontekst dla drugiego AI, **co** ta
      technologia robi w systemie, a nie tylko ze jest uzyta).

    Brak pola sprawdzamy przez ``note.frontmatter`` (raw dict) **oraz**
    typowane atrybuty ``VaultNote`` — zeby zlapac zarowno przypadek
    "klucz nie istnieje" jak i "klucz istnieje ale jest pusty / None".
    """

    violations: list[StructuralViolation] = []
    for path, note in notes_by_path.items():
        note_type = (note.type or "").strip()
        if not note_type:
            continue

        normalized_type = note_type.lower()

        if normalized_type == "hub":
            lower_tags = {t.lower() for t in note.tags}
            if "hub" not in lower_tags:
                violations.append(
                    StructuralViolation(
                        note_path=path,
                        note_type=note_type,
                        rule="hub_requires_hub_tag",
                        message=(
                            "Notatka typu 'hub' musi miec tag 'hub' we "
                            "frontmatterze (nawigacja po hubach opiera sie "
                            "na tym tagu)."
                        ),
                    )
                )

        if normalized_type == "decision":
            if not (note.parent or "").strip():
                violations.append(
                    StructuralViolation(
                        note_path=path,
                        note_type=note_type,
                        rule="decision_requires_parent",
                        message=(
                            "Notatka typu 'decision' (ADR) musi miec pole "
                            "'parent' wskazujace na hub tematyczny — "
                            "inaczej decyzja nie jest nigdzie zindeksowana."
                        ),
                    )
                )

        if normalized_type == "technology":
            role_value = note.frontmatter.get("role") if note.frontmatter else None
            if not (isinstance(role_value, str) and role_value.strip()):
                violations.append(
                    StructuralViolation(
                        note_path=path,
                        note_type=note_type,
                        rule="technology_requires_role",
                        message=(
                            "Notatka typu 'technology' musi miec pole "
                            "'role' we frontmatterze — krotki opis roli "
                            "technologii w systemie."
                        ),
                    )
                )

    violations.sort(key=lambda v: (v.note_path, v.rule))
    return violations


def _find_inconsistent_tags(
    notes_by_path: dict[str, VaultNote],
) -> list[TagInconsistency]:
    issues: list[TagInconsistency] = []
    for path, note in notes_by_path.items():
        note_type = (note.type or "").strip()
        if not note_type:
            continue
        if note_type.lower() == "moc":
            continue

        expected_tag = note_type.lower()
        if expected_tag in {tag.lower() for tag in note.tags}:
            continue

        issues.append(
            TagInconsistency(
                note_path=path,
                note_type=note_type,
                expected_tag=expected_tag,
            )
        )

    issues.sort(key=lambda i: i.note_path)
    return issues
