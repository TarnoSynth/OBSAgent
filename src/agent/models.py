"""Modele danych warstwy agenta — stan miedzy biegami.

Ta warstwa jest **jedynym miejscem**, gdzie ``src/git`` i ``src/vault`` sie spotykaja.
Stan agenta trzyma:

- Co zostalo przetworzone z historii Gita (``processed_commits`` per repo)
- Zdjecie vaulta na koniec ostatniego biegu (``VaultSnapshot``)
- Kiedy ostatnio agent skonczyl prace (``last_run``)

Stan jest **pamiecia miedzy biegami**. Bez niego agent w kazdym uruchomieniu
analizowalby historie od nowa — duplikujac notatki i spalajac tokeny.

Pola ``VaultSnapshot`` sa celowo minimalne. To nie jest kopia vaulta, tylko
**metadane stanu koncowego** — pozwalaja wykryc roznice miedzy biegami
(np. user skasowal recznie notatke, dodal modul, zmienil changelog).

Warstwa nie zna konkretnego providera AI ani logiki biegu run() — to domena
``src.agent`` wyzej w lancuchu (Faza 6). Tu siedzi wylacznie kontrakt danych.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.vault.models import VaultKnowledge


_CHANGELOG_TYPE = "changelog"
_MODULE_TYPE = "module"


class VaultSnapshot(BaseModel):
    """Lekkie zdjecie vaulta na koniec ostatniego biegu agenta.

    Nie przechowujemy tu tresci notatek ani pelnego indeksu \u2014 to robi
    ``VaultKnowledge`` budowany na zywo przez ``VaultManager.scan_all``.
    Snapshot trzyma **metadane**, ktore pozwalaja porownywac stany miedzy
    biegami bez koniecznosci rescanowania starego vaulta (nie mamy zreszta
    do niego dostepu post-factum).

    Konkretne uzycia w Fazie 6:

    - ``total_notes`` z aktualnego ``scan_all`` vs snapshot \u2014 wykrywa ile
      notatek przybylo / ubylo miedzy biegami
    - ``modules`` \u2014 wykrywa nowe / usuniete moduly bez koniecznosci
      analizy samego diffa
    - ``last_changelog`` \u2014 wskazuje na najswiezszy dziennik do ewentualnego
      dopisania (zamiast tworzenia nowego pliku co bieg)
    """

    total_notes: int = 0
    modules: list[str] = Field(default_factory=list)
    """Stemy notatek typu ``module`` (bez ``.md``). Gotowe do uzycia jako
    wikilink target ``[[X]]``. Posortowane alfabetycznie dla determinizmu."""

    last_changelog: str = ""
    """Sciezka relatywna do najswiezszej notatki typu ``changelog`` w momencie
    snapshotu (wybrana po ``modified`` z fallbackiem na ``created``). Pusty
    string gdy w vaulcie nie ma zadnej notatki typu ``changelog``."""

    @classmethod
    def from_knowledge(cls, knowledge: "VaultKnowledge") -> "VaultSnapshot":
        """Buduje snapshot z ``VaultKnowledge`` \u2014 pure function, bez I/O.

        Wywolywana przez agenta (Faza 6) po ``VaultManager.scan_all`` na
        koncu biegu, tuz przed zapisaniem ``AgentState``. Nie modyfikuje
        argumentu, nie dotyka plikow.
        """

        module_notes = knowledge.find_by_type(_MODULE_TYPE)
        module_stems = sorted({Path(note.path).stem for note in module_notes})

        changelog_notes = knowledge.find_by_type(_CHANGELOG_TYPE)
        last_changelog = ""
        if changelog_notes:
            def _sort_key(note):
                return (
                    note.modified or note.created or datetime.min,
                    note.path,
                )
            newest = max(changelog_notes, key=_sort_key)
            last_changelog = newest.path

        return cls(
            total_notes=knowledge.total_notes,
            modules=module_stems,
            last_changelog=last_changelog,
        )


class AgentState(BaseModel):
    """Stan agenta zapisywany miedzy biegami w ``.agent-state.json``.

    **Kontrakt kluczy ``processed_commits``:**

    - ``"project"`` \u2014 SHA commitow z repo projektu (kod)
    - ``"vault"``   \u2014 SHA commitow z repo vaulta (recznie edytowana
      dokumentacja, ktora agent juz uwzglednil)

    Lista per repo jest **ograniczona** w czasie zapisu do ostatnich N SHA
    (domyslnie 20, konfigurowalne w ``AgentStateStore``). Pozwala to na
    deduplikacje w ``GitReader.get_commits_since_last_run`` bez
    nieograniczonego rozrostu pliku.

    **Brak pliku stanu = brak ``AgentState``**. ``AgentStateStore.load``
    zwraca wtedy ``None``, a decyzja "co zrobic na pierwszym biegu"
    (analizuj N ostatnich) zostaje po stronie agenta (Faza 6), nie stanu.
    """

    last_run: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    """Timestamp zakonczenia ostatniego biegu (UTC z tzinfo). Uzywany przez
    agenta jako wtorny filtr temporalny w ``GitReader`` \u2014 obok
    ``processed_commits`` jako glowny filtr deduplikacyjny."""

    processed_commits: dict[str, list[str]] = Field(
        default_factory=lambda: {"project": [], "vault": []}
    )
    """Mapa repo \u2192 lista SHA przetworzonych commitow. Klucze: ``"project"``
    i ``"vault"``. Listy sa trimowane do ostatnich N przy zapisie przez
    ``AgentStateStore.save``."""

    vault_snapshot: VaultSnapshot = Field(default_factory=VaultSnapshot)
    """Zdjecie vaulta z konca ostatniego biegu \u2014 patrz ``VaultSnapshot``."""

    def touch(self) -> None:
        """Aktualizuje ``last_run`` na aktualny czas UTC \u2014 pomocnik dla agenta
        wywolywany tuz przed zapisem stanu."""

        self.last_run = datetime.now(timezone.utc)

    def mark_processed(self, repo: str, shas: list[str]) -> None:
        """Dopisuje nowe SHA do ``processed_commits[repo]`` zachowujac unikalnosc.

        Nowe SHA trafiaja na **poczatek** listy (najnowsze pierwsze). Trim do
        rozmiaru okna robi dopiero ``AgentStateStore.save`` \u2014 tutaj trzymamy
        dokladnie to, co agent chcial zapamietac. Dzieki temu kolejnosc
        wywolan w petli agenta nie wplywa na finalny stan.
        """

        if repo not in self.processed_commits:
            self.processed_commits[repo] = []

        existing = set(self.processed_commits[repo])
        prepend = [sha for sha in shas if sha and sha not in existing]
        self.processed_commits[repo] = prepend + self.processed_commits[repo]
