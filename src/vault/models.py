"""Modele danych warstwy vault (semantyka Obsidiana).

Zawiera:

- ``VaultNote``      — sparsowana pojedyncza notatka .md z typowanym frontmatterem
- ``VaultKnowledge`` — **search index** po calym vaulcie, uzywany przez zewnetrznego
  agenta AI (coding assistant) do szybkiego przeszukiwania wiedzy

Modele sa nieswiadome Gita i historii zmian. Do opisywania "co sie zmienilo"
agent uzywa ``CommitInfo`` z warstwy git i doczytuje aktualna tresc plikow
przez ``VaultManager.read_note``.

``AgentAction`` (decyzja AI) zyje w ``src.agent.models`` — to kontrakt warstwy
agenta, nie vaulta.

Schemat frontmattera (kontrakt dla coding assistanta — musi byc w KAZDEJ notatce):

    ---
    tags:    [alpha, beta]          # lista tagow
    type:    ADR                     # rodzaj: ADR / changelog / module / doc / MOC
    parent:  "[[MOC__Core]]"         # wikilink do nadrzednej notatki / MOC
    related: ["[[Auth]]", "[[JWT]]"] # lista powiazanych wikilinkow
    status:  active                  # active / archived / draft / deprecated
    created: 2025-04-10              # data utworzenia
    updated: 2025-04-12              # ostatnia recznie oznaczona aktualizacja
    modified: 2025-04-15T18:22       # ostatnia modyfikacja (plugin Obsidian Git)
    ---

Dzieki temu schematowi coding assistant potrafi:

- ``knowledge.find_by_type("ADR")``       → wszystkie decyzje architektoniczne
- ``knowledge.find_by_tag("auth")``       → wszystko o autoryzacji
- ``knowledge.children_of("MOC__Core")``  → co siedzi w tym MOC-u
- ``knowledge.backlinks_to("Auth")``      → kto linkuje do notatki Auth
- ``knowledge.related_to("Auth")``        → notatki powiazane przez pole ``related``
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class VaultNote(BaseModel):
    """Pojedyncza notatka w vaulcie po sparsowaniu frontmattera i tresci.

    Typowane pola odpowiadaja 1:1 kluczom YAML (``type``, ``parent``, ``related``,
    ``status``, ``created``, ``updated``, ``modified``). ``parent`` i ``related``
    sa juz stripnięte z nawiasow ``[[ ]]`` i gotowe do uzycia jako identyfikatory
    notatek w indeksach wyszukiwania.

    Surowy ``frontmatter`` dict jest zachowany jako escape hatch dla
    niestandardowych kluczy, ktorych coding assistant moze potrzebowac.
    """

    path: str
    title: str
    content: str
    frontmatter: dict = Field(default_factory=dict)

    tags: list[str] = Field(default_factory=list)
    type: str | None = None
    parent: str | None = None
    related: list[str] = Field(default_factory=list)
    status: str | None = None

    created: datetime | None = None
    updated: datetime | None = None
    modified: datetime | None = None

    wikilinks: list[str] = Field(default_factory=list)

class VaultKnowledge(BaseModel):
    """Search index po calym vaulcie — API dla coding assistanta.

    Wszystkie indeksy sa zbudowane raz w ``VaultManager.scan_all`` i udostepnione
    przez metody pomocnicze. Kazda metoda ``find_*`` / ``children_of`` /
    ``backlinks_to`` / ``related_to`` zwraca pelne ``VaultNote`` (O(1) po indeksie).

    Nazwy notatek w kluczach indeksow sa stripnięte: bez ``[[ ]]``, bez sekcji
    ``#anchor``, bez aliasu ``|display`` i bez rozszerzenia ``.md``. Metody
    zapytaniowe normalizuja argument tak samo, wiec oba warianty dzialaja:

        knowledge.backlinks_to("Auth")
        knowledge.backlinks_to("[[Auth#Section]]")
    """

    total_notes: int = 0
    notes: list[VaultNote] = Field(default_factory=list)

    all_tags: set[str] = Field(default_factory=set)
    all_wikilinks: set[str] = Field(default_factory=set)
    moc_files: list[str] = Field(default_factory=list)
    orphaned_links: list[str] = Field(default_factory=list)

    by_path: dict[str, VaultNote] = Field(default_factory=dict)
    by_stem: dict[str, list[str]] = Field(default_factory=dict)
    by_type: dict[str, list[str]] = Field(default_factory=dict)
    by_tag: dict[str, list[str]] = Field(default_factory=dict)
    by_status: dict[str, list[str]] = Field(default_factory=dict)
    children_index: dict[str, list[str]] = Field(default_factory=dict)
    backlinks_index: dict[str, list[str]] = Field(default_factory=dict)
    related_index: dict[str, list[str]] = Field(default_factory=dict)

    def get(self, path: str) -> VaultNote | None:
        """Zwraca notatke po dokladnej sciezce wzglednej (lub ``None``)."""

        return self.by_path.get(path)

    def resolve(self, ref: str) -> VaultNote | None:
        """Rozwiazuje dowolna referencje do notatki — glowna metoda nawigacji po grafie.

        Agent widzi w tresci ``[[Auth]]`` i chce pobrac cala notatke:
        ``knowledge.resolve("[[Auth]]")``. Akceptuje:

        - ``"[[Auth]]"``, ``"[[Auth#Section|alias]]"`` — wikilinki
        - ``"Auth"`` — sam stem
        - ``"Auth.md"`` — nazwa pliku
        - ``"modules/Auth"`` lub ``"modules/Auth.md"`` — sciezka z folderem

        Priorytet: exact path → stem (pierwsza posortowanym path-em jesli wiele).
        Zwraca ``None`` jesli nic nie pasuje.
        """

        key = self._normalize_ref(ref)
        if not key:
            return None

        candidate_paths = [f"{key}.md", key]
        for path in candidate_paths:
            if path in self.by_path:
                return self.by_path[path]

        stem = key.rsplit("/", 1)[-1]
        paths = self.by_stem.get(stem, [])
        if paths:
            return self.by_path[paths[0]]

        return None

    def mocs(self) -> list[VaultNote]:
        """Wszystkie MOC-i jako pelne ``VaultNote``.

        Unia dwoch kryteriow: nazwa pliku zaczynajaca sie od ``MOC__`` **lub**
        ``type: MOC`` we frontmatterze. Dzieki temu wykryjemy MOC-i nawet gdy
        ktorys wariant zostal pominiety przez AI przy tworzeniu notatki.
        """

        seen: set[str] = set()
        result: list[VaultNote] = []
        for path in self.moc_files:
            note = self.by_path.get(path)
            if note and note.path not in seen:
                seen.add(note.path)
                result.append(note)
        for note in self.by_path.values():
            if note.type == "MOC" and note.path not in seen:
                seen.add(note.path)
                result.append(note)
        result.sort(key=lambda n: n.path)
        return result

    def find_by_type(self, note_type: str) -> list[VaultNote]:
        """Wszystkie notatki o danym ``type`` (np. ``"ADR"``, ``"module"``, ``"changelog"``)."""

        return self._notes_for(self.by_type.get(note_type, []))

    def find_by_tag(self, tag: str) -> list[VaultNote]:
        """Wszystkie notatki z danym tagiem (akceptuje ``"auth"`` i ``"#auth"``)."""

        return self._notes_for(self.by_tag.get(tag.lstrip("#"), []))

    def find_by_status(self, status: str) -> list[VaultNote]:
        """Wszystkie notatki o danym statusie (``active``, ``archived``, ``draft``, ...)."""

        return self._notes_for(self.by_status.get(status, []))

    def children_of(self, parent: str) -> list[VaultNote]:
        """Notatki ktore maja ``parent == <parent>`` we frontmatterze.

        Typowe uzycie: ``children_of("MOC__Core")`` → cala zawartosc MOC-a.
        """

        return self._notes_for(self.children_index.get(self._normalize_ref(parent), []))

    def backlinks_to(self, target: str) -> list[VaultNote]:
        """Notatki ktore linkuja do ``target`` przez wikilink ``[[target]]`` w tresci."""

        return self._notes_for(self.backlinks_index.get(self._normalize_ref(target), []))

    def related_to(self, target: str) -> list[VaultNote]:
        """Notatki ktore maja ``target`` w swoim polu ``related`` (graf symetryczny)."""

        return self._notes_for(self.related_index.get(self._normalize_ref(target), []))

    def connected_to(self, target: str) -> list[VaultNote]:
        """Wszystkie notatki **powiazane** z ``target`` w grafie — jedna unia:

        - sama notatka ``target`` (jesli istnieje jako plik)
        - notatki z ``parent == target`` (children_of)
        - notatki z ``target`` w polu ``related`` (related_to)
        - notatki z wikilinkiem ``[[target]]`` w tresci (backlinks_to)

        Typowe uzycie: agent chce "wszystko o SQLAlchemy" i dostaje komplet
        w jednym wywolaniu. Wynik zdeduplikowany, posortowany po sciezce.
        Dziala takze gdy ``target`` nie jest odrebna notatka (np. koncepcja /
        tag-jak-notatka) — zwroci same krawedzie grafu bez subjectu.
        """

        seen: set[str] = set()
        result: list[VaultNote] = []

        subject = self.resolve(target)
        if subject is not None:
            seen.add(subject.path)
            result.append(subject)

        for group in (
            self.children_of(target),
            self.related_to(target),
            self.backlinks_to(target),
        ):
            for note in group:
                if note.path not in seen:
                    seen.add(note.path)
                    result.append(note)

        result.sort(key=lambda n: n.path)
        return result

    def _notes_for(self, paths: list[str]) -> list[VaultNote]:
        return [self.by_path[p] for p in paths if p in self.by_path]

    @staticmethod
    def _normalize_ref(name: str) -> str:
        """Normalizuje referencje: ``[[Auth#Section|alias]]`` → ``Auth``, ``foo.md`` → ``foo``."""

        text = name.strip()
        if text.startswith("[[") and text.endswith("]]"):
            text = text[2:-2]
        text = text.split("|", 1)[0].split("#", 1)[0].strip()
        if text.endswith(".md"):
            text = text[:-3]
        return text

