"""MOCManager — logika utrzymania MOC-ow i ``_index.md`` (Faza 4b).

Cel warstwy:

- Po kazdej zmianie w vaulcie graf wiedzy musi zostac spojny: nowa notatka
  powinna byc dolinkowana z odpowiedniego MOC-a, a globalny ``_index.md``
  musi widziec ja w sekcji wlasciwego typu.
- ``MOCManager`` realizuje **wylacznie deterministyczna logike utrzymania**.
  Nie podejmuje decyzji semantycznych typu "jak nazwac sekcje" ani "co
  zalezy od czego" — to zostawione agentowi (Faza 6).

Co NIE nalezy do tej klasy:

- Wybor providera AI / generowanie tresci notatek
- Sync z Gitem (commit / push)
- Czytanie historii zmian — ``MOCManager`` operuje na **aktualnym** stanie
  vaulta przez ``VaultManager`` i opcjonalnie na zbudowanym ``VaultKnowledge``

Wszystkie operacje sa **idempotentne**: ponowne wywolanie z tym samym
inputem nie zmienia pliku jesli wpis juz istnieje. Dzieki temu agent moze
wolac ``ensure_note_in_moc`` po kazdej akcji bez ryzyka duplikatow.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from src.vault.manager import VaultManager
from src.vault.models import VaultKnowledge, VaultNote

logger = logging.getLogger(__name__)

DEFAULT_INDEX_PATH = "_index.md"

#: Domyslny wzorzec nazewniczy MOC-ow. Potrojne podkreslenie jest konwencja
#: AthleteStack (``MOC___Kompendium``, ``MOC___Architektura``). User moze
#: zmienic wzorzec w ``config.yaml`` (``vault.moc_pattern``).
DEFAULT_MOC_PATTERN = "MOC___{name}"

#: Prefiks legacy **usuniety w Fazie 7** — ``MOC__X`` (podwojne
#: podkreslenie) nie jest juz rozpoznawane jako MOC. Vault po czyszczeniu
#: (patrz REFACTOR_PLAN.md) uzywa wylacznie ``MOC___{name}``. Stała
#: zostala jako placeholder dla wstecznej kompatybilnosci importow — nie
#: uzywaj w nowym kodzie.
LEGACY_MOC_PREFIX = "MOC__"

#: Token w ``moc_pattern`` oznaczajacy miejsce nazwy/labela MOC-a. Wzorzec
#: MUSI zawierac ten token dokladnie raz \u2014 walidowane w ``_split_moc_pattern``.
_MOC_PATTERN_TOKEN = "{name}"

_OTHER_SECTION = "Other"
_MOC_SECTION = "MOCs"

_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

#: Mapa ``note.type`` -> tytul sekcji w bootstrap MOC-u.
#: MUSI trzymac sie 1:1 z sekcjami wygenerowanymi przez ``render_bootstrap_moc``
#: — jesli tu zmienimy naglowki, trzeba i tam.
MOC_SECTION_TITLES: dict[str, dict[str, str]] = {
    "pl": {
        "module": "Moduly",
        "hub": "Huby",
        "technology": "Technologie",
        "decision": "Decyzje architektoniczne",
        "adr": "Decyzje architektoniczne",
        "concept": "Koncepty",
    },
    "en": {
        "module": "Modules",
        "hub": "Hubs",
        "technology": "Technologies",
        "decision": "Architectural decisions",
        "adr": "Architectural decisions",
        "concept": "Concepts",
    },
}
MOC_SECTION_FALLBACK_PL = "Inne"
MOC_SECTION_FALLBACK_EN = "Other"

#: Placeholder bootstrap-u (``_(pusto — ...)_`` / ``_(empty — ...)_``). Jesli
#: sekcja ma tylko placeholder, przed wrzuceniem pierwszego wpisu placeholder
#: jest usuwany — zeby lista nie wygladala jak "placeholder + link".
_PLACEHOLDER_LINE_RE = re.compile(r"^_\(\s*(pusto|empty)\b[^)]*\)_\s*$")


def moc_section_for_type(note_type: str | None, language: str = "pl") -> str:
    """Mapuje ``note.type`` na tytul sekcji w bootstrap MOC-u.

    ``None`` / brak mapowania -> sekcja fallback (``Inne`` / ``Other``).
    Case-insensitive na kluczu.
    """

    lang = language if language in MOC_SECTION_TITLES else "pl"
    table = MOC_SECTION_TITLES[lang]
    fallback = MOC_SECTION_FALLBACK_PL if lang == "pl" else MOC_SECTION_FALLBACK_EN
    if not note_type:
        return fallback
    key = note_type.strip().lower()
    return table.get(key, fallback)


def insert_into_moc_section(raw: str, section: str, stem: str) -> str:
    """Wklada ``- [[stem]]`` do podanej sekcji ``## {section}`` w MOC-u.

    Zasady:

    - jesli sekcja istnieje i ma placeholder ``_(pusto — ...)_`` -> zastepuje
      placeholder linkiem (nie dubluje);
    - jesli istnieje i ma juz wpisy -> dopisuje na koncu sekcji;
    - jesli nie istnieje -> dodaje ``## {section}`` na koncu pliku z entry.

    Idempotencja: caller powinien sprawdzic ``_moc_contains_link`` PRZED
    wywolaniem — ta funkcja nie weryfikuje czy stem juz jest (zeby wstawiac
    swiadomie, nawet gdy ktos chce duplikat-by-design).
    """

    entry = f"- [[{stem}]]"
    lines = raw.splitlines()
    section_start: int | None = None
    section_level: int | None = None
    section_end: int | None = None

    for idx, line in enumerate(lines):
        match = _HEADING_LINE_RE.match(line)
        if not match:
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        if section_start is None and title == section:
            section_start = idx
            section_level = level
            continue
        if (
            section_start is not None
            and section_level is not None
            and level <= section_level
        ):
            section_end = idx
            break

    if section_start is None:
        prefix = "" if raw.endswith("\n\n") or not raw else ("\n" if raw.endswith("\n") else "\n\n")
        return f"{raw}{prefix}## {section}\n\n{entry}\n"

    if section_end is None:
        section_end = len(lines)

    body_indices = list(range(section_start + 1, section_end))
    placeholder_idx: int | None = None
    for i in body_indices:
        if _PLACEHOLDER_LINE_RE.match(lines[i].strip()):
            placeholder_idx = i
            break

    if placeholder_idx is not None:
        lines[placeholder_idx] = entry
        new_raw = "\n".join(lines)
        if raw.endswith("\n") and not new_raw.endswith("\n"):
            new_raw += "\n"
        return new_raw

    insertion = section_end
    while insertion > section_start + 1 and not lines[insertion - 1].strip():
        insertion -= 1
    new_lines = lines[:insertion] + [entry] + lines[insertion:]
    new_raw = "\n".join(new_lines)
    if raw.endswith("\n") and not new_raw.endswith("\n"):
        new_raw += "\n"
    return new_raw


def moc_contains_link(content: str, stem: str) -> bool:
    """Czy tresc MOC zawiera juz wikilink ``[[stem]]`` lub ``[[stem|...]]``."""

    target = re.escape(stem)
    pattern = re.compile(r"\[\[" + target + r"(\|[^\]]*)?(#[^\]]*)?\]\]")
    return bool(pattern.search(content))


def _split_moc_pattern(pattern: str) -> tuple[str, str]:
    """Rozbija wzorzec ``MOC___{name}`` na (prefix, suffix).

    Wzorzec MUSI zawierac ``{name}`` dokladnie raz. Prefix to wszystko przed
    tokenem, suffix \u2014 wszystko po. Obecnie uzywany tylko prefix (suffix
    zarezerwowany pod przyszle konwencje typu ``MOC___{name}.hub``).

    Rzuca ``ValueError`` gdy wzorzec niepoprawny \u2014 walidacja configu tego
    nie przepusci.
    """

    if not isinstance(pattern, str) or not pattern:
        raise ValueError("moc_pattern musi byc niepustym stringiem")
    if pattern.count(_MOC_PATTERN_TOKEN) != 1:
        raise ValueError(
            f"moc_pattern musi zawierac token '{_MOC_PATTERN_TOKEN}' dokladnie raz, "
            f"dostalismy {pattern!r}"
        )
    prefix, _, suffix = pattern.partition(_MOC_PATTERN_TOKEN)
    if not prefix:
        raise ValueError(
            f"moc_pattern musi miec niepusty prefix przed '{_MOC_PATTERN_TOKEN}', "
            f"dostalismy {pattern!r}"
        )
    return prefix, suffix


class BootstrapMocOutcome(BaseModel):
    """Wynik wywolania ``MOCManager.ensure_bootstrap_moc``.

    ``result``:

    - ``created`` — plik MOC nie istnial i zostal utworzony na podstawie aktualnego
      skanu vaulta (wszystkie notatki wskazujace ``parent: [[MOC]]`` albo pasujace
      do heurystyki grafu)
    - ``merged`` — plik MOC istnial i dopisalismy do niego brakujace linki
      (nigdy nie usuwamy tresci — czysty top-up)
    - ``already_present`` — plik MOC istnial i wszystkie oczekiwane linki juz tam byly
    - ``is_not_a_moc`` — plik pod docelowa sciezka istnieje, ale nie jest MOC-iem
      (np. podmienione przez usera) — bez nadpisywania, zostawiamy decyzje operatorowi

    ``added_links``: lista ``"[sekcja] - [[stem]]"`` dla kazdego dopisanego wpisu
    — uzywane w preview i commit messagu zeby user widzial co konkretnie przybylo.
    """

    path: str
    label: str
    result: Literal["created", "merged", "already_present", "is_not_a_moc"]
    content: str | None = None
    added_links: list[str] = Field(default_factory=list)


class MOCLinkOutcome(BaseModel):
    """Wynik proby dolinkowania notatki do MOC-a.

    ``result``:

    - ``added`` — link zostal dopisany do MOC
    - ``already_present`` — MOC juz linkowal do notatki, nic nie zmieniono
    - ``no_moc_found`` — nie znaleziono pasujacego MOC-a wedlug heurystyki
    - ``skipped_is_moc`` — notatka jest sama MOC-iem, MOC-i sie nie linkuje
      do innych MOC-ow przez te metode
    """

    note_path: str
    result: Literal["added", "already_present", "no_moc_found", "skipped_is_moc"]
    moc_path: str | None = None
    detail: str | None = None


class IndexUpdateOutcome(BaseModel):
    """Wynik aktualizacji ``_index.md``.

    ``changed`` mowi czy plik zostal zmodyfikowany (False = juz spojny).
    ``section`` to naglowek pod ktorym notatka zostala zaindeksowana.
    """

    note_path: str
    index_path: str
    section: str
    changed: bool
    created_index: bool = False


class MOCManager:
    """Logika utrzymania MOC-ow i indeksu projektu.

    Klasa **uzywa** ``VaultManager`` jako warstwy dostepu do plikow — sama nie
    sieg po dysk inaczej niz przez te warstwe.

    Heurystyka ``find_moc_for_note`` jest deterministyczna i probuje strategii
    od najmocniejszej do najslabszej:

    1. ``note.parent`` (jesli wskazuje na istniejacy MOC)
    2. MOC w tym samym **podfolderze** co notatka (root vaulta wykluczony —
       byl by zbyt szeroki: kazda notatka w roocie i kazdy MOC w roocie by sie
       trafialy nawzajem przypadkowo)
    3. MOC nazwany ``MOC__<X>`` gdzie ``X`` (case-insensitive) pasuje do tagu notatki
    4. MOC nazwany ``MOC__<Type>`` lub ``MOC__<Type>s`` pasujacy do typu notatki

    Zwraca ``None`` jesli nic nie pasuje — wtedy agent dostaje sygnal
    ``no_moc_found`` i moze sam zdecydowac co dalej (utworzyc MOC, pominac, itp.).
    """

    def __init__(
        self,
        vault_manager: VaultManager,
        *,
        moc_pattern: str = DEFAULT_MOC_PATTERN,
    ) -> None:
        """Tworzy managera MOC-ow ze sparametryzowanym wzorcem nazewniczym.

        ``moc_pattern`` - format nazwy MOC z ``{name}`` jako placeholder labela.
        Domyslnie ``"MOC___{name}"`` (konwencja AthleteStack, Faza 0).

        **Faza 7:** legacy fallback ``MOC__X`` (podwojne podkreslenie)
        zostal usuniety. Jedyna rozpoznawana konwencja to ``moc_pattern``
        z configu + jawna deklaracja ``type: moc`` we frontmatterze.
        """

        self.vault_manager = vault_manager
        self._primary_prefix, self._primary_suffix = _split_moc_pattern(moc_pattern)
        self.moc_pattern = moc_pattern

    @classmethod
    def from_config(
        cls,
        vault_manager: VaultManager,
        config_path: "str | Path",
    ) -> "MOCManager":
        """Factory czytajace ``vault.moc_pattern`` z ``config.yaml``.

        Brak klucza albo brak sekcji ``vault`` \u2192 default ``DEFAULT_MOC_PATTERN``.
        Blednie uformowany wzorzec (brak ``{name}``) \u2192 ``ValueError`` z
        konkretnego miejsca konfigu.
        """

        import yaml  # noqa: PLC0415 \u2014 lokalny import, yaml nie jest w hot path.

        path = Path(config_path).expanduser().resolve()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        vault_cfg = raw.get("vault") if isinstance(raw, dict) else None
        pattern = DEFAULT_MOC_PATTERN
        if isinstance(vault_cfg, dict):
            raw_pattern = vault_cfg.get("moc_pattern")
            if raw_pattern is not None:
                if not isinstance(raw_pattern, str) or not raw_pattern.strip():
                    raise ValueError(
                        "config: vault.moc_pattern musi byc niepustym stringiem"
                    )
                pattern = raw_pattern.strip()
        return cls(vault_manager, moc_pattern=pattern)

    def find_moc_for_note(
        self,
        note: VaultNote,
        knowledge: VaultKnowledge | None = None,
    ) -> VaultNote | None:
        """Wybiera **jeden** MOC najlepiej pasujacy do podanej notatki.

        Jesli notatka sama jest MOC-iem zwraca ``None`` (MOC-i nie sa dolinkowywane
        do innych MOC-ow ta sciezka).
        """

        knowledge = knowledge or self.vault_manager.scan_all()

        if self._is_moc_note(note):
            return None

        if note.parent:
            parent_note = knowledge.resolve(note.parent)
            if parent_note is not None and self._is_moc_note(parent_note):
                return parent_note

        mocs = knowledge.mocs()
        if not mocs:
            return None

        note_dir = Path(note.path).parent
        if note_dir != Path("."):
            for moc in mocs:
                if Path(moc.path).parent == note_dir and moc.path != note.path:
                    return moc

        note_tags_lower = {tag.lower() for tag in note.tags}
        if note_tags_lower:
            for moc in mocs:
                label = self._moc_label(moc)
                if label and label.lower() in note_tags_lower:
                    return moc

        if note.type:
            type_lower = note.type.lower()
            type_candidates = {type_lower, f"{type_lower}s"}
            for moc in mocs:
                label = self._moc_label(moc)
                if label and label.lower() in type_candidates:
                    return moc

        return None

    def ensure_note_in_moc(
        self,
        note: VaultNote,
        knowledge: VaultKnowledge | None = None,
    ) -> MOCLinkOutcome:
        """Dolinkowuje notatke do dopasowanego MOC-a — idempotentne.

        Kolejnosc:

        1. Notatki bedace MOC-ami sa pomijane (``skipped_is_moc``).
        2. Szukamy MOC-a heurystyka ``find_moc_for_note``.
        3. Re-czytamy MOC z dysku (swiezy stan, nie z cache ``knowledge``)
           — chroni przed nadpisaniem rownoleglych zmian usera.
        4. Jesli MOC juz linkuje do notatki przez ``[[stem]]`` w tresci —
           zwracamy ``already_present``.
        5. W przeciwnym razie dopisujemy ``- [[stem]]`` na koncu MOC.
        """

        if self._is_moc_note(note):
            return MOCLinkOutcome(
                note_path=note.path,
                result="skipped_is_moc",
                detail="Notatka jest MOC-iem — nie dolinkowuje do innego MOC.",
            )

        moc = self.find_moc_for_note(note, knowledge=knowledge)
        if moc is None:
            logger.info("MOCManager: nie znaleziono MOC dla %s", note.path)
            return MOCLinkOutcome(
                note_path=note.path,
                result="no_moc_found",
                detail="Heurystyka nie dopasowala zadnego MOC.",
            )

        fresh_moc = self.vault_manager.read_note(moc.path)
        note_stem = Path(note.path).stem
        if note_stem in fresh_moc.wikilinks:
            return MOCLinkOutcome(
                note_path=note.path,
                result="already_present",
                moc_path=moc.path,
            )

        link_line = f"- [[{note_stem}]]"
        self.vault_manager.append(moc.path, link_line)
        logger.info("MOCManager: dopisano %s do %s", note_stem, moc.path)

        return MOCLinkOutcome(
            note_path=note.path,
            result="added",
            moc_path=moc.path,
            detail=f"Dopisano '{link_line}' do {moc.path}.",
        )

    def update_index(
        self,
        note: VaultNote,
        *,
        index_path: str = DEFAULT_INDEX_PATH,
    ) -> IndexUpdateOutcome:
        """Dopisuje wpis ``- [[stem]]`` do sekcji odpowiadajacej typowi notatki.

        Zachowanie:

        - Jesli ``_index.md`` nie istnieje — tworzy go z minimalnym szkieletem
          zawierajacym jedna sekcje + ten wpis.
        - Jesli sekcja istnieje — wpis dopisywany jest na koncu sekcji
          (przed kolejnym naglowkiem rownej / wyzszej rangi).
        - Jesli sekcji nie ma — dolaczana jest na koncu pliku.
        - Idempotentne: jesli wpis juz jest w pliku, zwraca ``changed=False``.
        """

        section = self._section_title_for(note)
        entry = f"- [[{Path(note.path).stem}]]"

        if not self.vault_manager.note_exists(index_path):
            initial = self._render_initial_index(section, entry)
            self.vault_manager.create(index_path, initial)
            logger.info("MOCManager: utworzono %s + %s", index_path, entry)
            return IndexUpdateOutcome(
                note_path=note.path,
                index_path=index_path,
                section=section,
                changed=True,
                created_index=True,
            )

        raw = self.vault_manager.read_text(index_path)
        if self._entry_already_in_section(raw, section, entry):
            return IndexUpdateOutcome(
                note_path=note.path,
                index_path=index_path,
                section=section,
                changed=False,
            )

        new_raw = self._insert_entry_into_index(raw, section, entry)
        if new_raw == raw:
            return IndexUpdateOutcome(
                note_path=note.path,
                index_path=index_path,
                section=section,
                changed=False,
            )

        self.vault_manager.write_text(index_path, new_raw)
        logger.info("MOCManager: %s → sekcja '%s' += %s", index_path, section, entry)
        return IndexUpdateOutcome(
            note_path=note.path,
            index_path=index_path,
            section=section,
            changed=True,
        )

    def rebuild_index(
        self,
        *,
        index_path: str = DEFAULT_INDEX_PATH,
        knowledge: VaultKnowledge | None = None,
    ) -> IndexUpdateOutcome:
        """Pelna przebudowa ``_index.md`` ze swiezego skanu vaulta.

        Sortuje notatki po sciezce i grupuje wedlug ``type`` (MOC-i osobna
        sekcja, brak typu → ``Other``). Idempotentna — jesli wynik renderowania
        jest identyczny z aktualnym plikiem, ``changed=False``.
        """

        knowledge = knowledge or self.vault_manager.scan_all()
        rendered = self._render_full_index(knowledge, index_path=index_path)

        if self.vault_manager.note_exists(index_path):
            current = self.vault_manager.read_text(index_path)
            if current == rendered:
                return IndexUpdateOutcome(
                    note_path=index_path,
                    index_path=index_path,
                    section="*",
                    changed=False,
                )

        self.vault_manager.write_text(index_path, rendered)
        logger.info("MOCManager: przebudowano %s (%d notatek)", index_path, knowledge.total_notes)
        return IndexUpdateOutcome(
            note_path=index_path,
            index_path=index_path,
            section="*",
            changed=True,
            created_index=not self.vault_manager.note_exists(index_path),
        )

    def validate_orphaned_links(
        self,
        knowledge: VaultKnowledge | None = None,
    ) -> list[str]:
        """Zwraca liste **martwych** wikilinkow w vaulcie i loguje WARNING.

        "Martwy" = wikilink ``[[X]]`` w jakiejs notatce, dla ktorego nie
        istnieje plik ``X.md`` ani notatka o stem ``X``. Sygnal dla agenta /
        usera ze graf jest niespojny — bez side-effectow naprawy.
        """

        knowledge = knowledge or self.vault_manager.scan_all()
        orphans = list(knowledge.orphaned_links)
        if orphans:
            logger.warning(
                "MOCManager: wykryto %d osieroconych wikilinkow: %s",
                len(orphans),
                ", ".join(orphans[:10]) + ("..." if len(orphans) > 10 else ""),
            )
        return orphans

    def moc_path_for(self, name: str) -> str:
        """Zwraca relatywna sciezke ``.md`` MOC-a dla podanego labela.

        Uzywa aktualnego ``moc_pattern`` — np. dla ``name='Kompendium'`` i
        ``moc_pattern='MOC___{name}'`` zwroci ``'MOC___Kompendium.md'``.

        Rzuca ``ValueError`` gdy ``name`` jest pusty / ma whitespace w srodku.
        """

        if not isinstance(name, str) or not name.strip():
            raise ValueError("ensure_bootstrap_moc.name musi byc niepustym stringiem")
        stripped = name.strip()
        if any(ch.isspace() for ch in stripped):
            raise ValueError(
                f"ensure_bootstrap_moc.name nie moze miec whitespace ({stripped!r}) — "
                "stem pliku uzywamy 1:1 jako name w wikilinkach"
            )
        stem = self.moc_pattern.format(name=stripped)
        return f"{stem}.md"

    def ensure_bootstrap_moc(
        self,
        *,
        name: str,
        title: str | None = None,
        language: str = "pl",
        today: date | None = None,
        knowledge: VaultKnowledge | None = None,
    ) -> BootstrapMocOutcome:
        """Zapewnia ze root-MOC vaulta istnieje i zawiera wszystkie zywe notatki.

        Rozwiazuje dwa problemy na raz:

        1) **Pusty vault ma martwy parent** — wszystkie notatki z
           ``parent: [[MOC___Kompendium]]`` wskazuja na nieistniejacy plik.
           Po tym wywolaniu MOC istnieje i linkuje wszystkie dzieci.
        2) **MOC zostal utworzony szkieletem, a potem dochodzily notatki**
           (np. moc_planner miawal bug "nie dopisuje gdy parent ustawiony").
           Ten bootstrap przy kazdym starcie skanuje vault i dokleja brakujace
           linki (top-up, NIGDY nie usuwa). Dzieki temu nawet stare notatki
           wracaja do grafu bez osobnego rebuild-a.

        Algorytm:

        1. Skan vaulta (``knowledge`` albo ``vault_manager.scan_all``).
        2. Zbior kandydatow = wszystkie notatki z ``parent == "<MOC_stem>"``.
           Plus fallback: notatki z ``type`` ktore mamy zmapowane na sekcje
           MOC (module/hub/technology/decision/concept), ktore nie maja parenta
           w ogole — heurystyka "to nalezy do glownego MOC-a".
        3. Wyliczamy oczekiwany (stem, sekcja) per kandydata.
        4. Jesli plik MOC nie istnieje → renderujemy szkielet, wstrzykujemy
           wpisy, ``create`` na dysk.
        5. Jesli plik MOC istnieje i jest MOC-iem → dla kazdego wpisu
           sprawdzamy ``moc_contains_link(content, stem)``; brakujace
           wstrzykujemy przez ``insert_into_moc_section`` (placeholder zastapiony
           pierwszym linkiem, kolejne doklejane). Jesli zero zmian ->
           ``already_present``, jesli cokolwiek dopisane -> ``merged``.
        6. Jesli plik istnieje ale nie jest MOC-iem → ``is_not_a_moc``, zero zmian.

        :param name: label MOC-a (np. ``"Kompendium"``).
        :param title: naglowek ``# ...``. ``None`` -> ``"MOC — {name}"``.
        :param language: ``"pl"`` / ``"en"`` - wybor tytulow sekcji.
        :param today: data do pola ``created`` / ``updated``.
        :param knowledge: opcjonalnie wstrzykniety snapshot vaulta. Jesli None,
            funkcja woluje ``scan_all`` sama (nieco wolniej, ale niezaleznie).
        """

        rel_path = self.moc_path_for(name)
        label = name.strip()
        moc_stem = Path(rel_path).stem

        if knowledge is None:
            knowledge = self.vault_manager.scan_all()

        by_section = self._collect_moc_entries(
            knowledge=knowledge,
            moc_stem=moc_stem,
            language=language,
        )

        if not self.vault_manager.note_exists(rel_path):
            today_date = today or datetime.now(timezone.utc).date()
            content = render_bootstrap_moc(
                name=label,
                title=title,
                language=language,
                created=today_date,
                sections=by_section,
            )
            self.vault_manager.create(rel_path, content)

            total = sum(len(v) for v in by_section.values())
            added = [
                f"[{section}] - [[{stem}]]"
                for section, stems in by_section.items()
                for stem in stems
            ]
            logger.info(
                "ensure_bootstrap_moc: utworzono %s z %d linkami",
                rel_path, total,
            )
            return BootstrapMocOutcome(
                path=rel_path,
                label=label,
                result="created",
                content=content,
                added_links=added,
            )

        try:
            existing = self.vault_manager.read_note(rel_path)
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "ensure_bootstrap_moc: %s istnieje ale parse padl (%r) - traktuje jako not-a-moc.",
                rel_path, exc,
            )
            return BootstrapMocOutcome(path=rel_path, label=label, result="is_not_a_moc")

        if not self._is_moc_note(existing):
            logger.warning(
                "ensure_bootstrap_moc: %s istnieje ale nie jest MOC-iem (type=%r) - nie nadpisuje.",
                rel_path, existing.type,
            )
            return BootstrapMocOutcome(path=rel_path, label=label, result="is_not_a_moc")

        raw = self.vault_manager.read_text(rel_path)
        new_raw = raw
        added: list[str] = []
        for section, stems in by_section.items():
            for stem in stems:
                if moc_contains_link(new_raw, stem):
                    continue
                candidate = insert_into_moc_section(new_raw, section, stem)
                if candidate != new_raw:
                    new_raw = candidate
                    added.append(f"[{section}] - [[{stem}]]")

        if not added:
            return BootstrapMocOutcome(
                path=rel_path,
                label=label,
                result="already_present",
                content=raw,
            )

        self.vault_manager.write_text(rel_path, new_raw)
        logger.info(
            "ensure_bootstrap_moc: merged %d linkow do %s",
            len(added), rel_path,
        )
        return BootstrapMocOutcome(
            path=rel_path,
            label=label,
            result="merged",
            content=new_raw,
            added_links=added,
        )

    def _collect_moc_entries(
        self,
        *,
        knowledge: VaultKnowledge,
        moc_stem: str,
        language: str,
    ) -> dict[str, list[str]]:
        """Zbiera wpisy ktore powinny byc w MOC-u pogrupowane po sekcji.

        Zrodla wpisow (w tej kolejnosci — duplikaty odfiltrowane przez
        sprawdzenie czy stem juz jest w by_section):

        1. notatki z ``note.parent == moc_stem`` — explicit: user/agent
           swiadomie wskazal ten MOC jako rodzica.
        2. notatki ktorych ``type`` mapuje sie na sekcje MOC-u (module/hub/
           technology/decision/concept) i ktore NIE maja parenta w ogole —
           heurystyka "zgubek": gdy agent zapomnial ustawic parent, ale typ
           jasno wskazuje ze to nalezy do glownego MOC-a.

        Wynik: dict z kluczami = tytul sekcji (PL albo EN), wartosci = lista
        stemow w kolejnosci sortowanej alfabetycznie (stabilny diff).
        """

        by_section: dict[str, list[str]] = {}
        seen: set[str] = set()

        def _add(note: VaultNote) -> None:
            stem = Path(note.path).stem
            if stem == moc_stem or stem in seen:
                return
            if self._is_moc_note(note):
                return
            if (note.type or "").lower() == "index":
                return
            section = moc_section_for_type(note.type, language)
            by_section.setdefault(section, []).append(stem)
            seen.add(stem)

        for note in knowledge.children_of(moc_stem):
            _add(note)

        valid_types = set(MOC_SECTION_TITLES["pl"].keys()) | set(MOC_SECTION_TITLES["en"].keys())
        for note in knowledge.notes:
            if note.parent:
                continue
            note_type = (note.type or "").strip().lower()
            if note_type not in valid_types:
                continue
            _add(note)

        for section in by_section:
            by_section[section] = sorted(by_section[section])

        return by_section

    def _is_moc_note(self, note: VaultNote) -> bool:
        """Czy notatka jest MOC-iem wedlug primary patternu lub frontmattera.

        Kolejnosc sprawdzania (Faza 7 — bez legacy fallback):

        1. ``note.type == "moc"`` (case-insensitive) — jawna deklaracja
           w frontmatterze wygrywa nad heurystyka nazwy.
        2. Stem pasuje do primary prefixu (np. ``MOC___Kompendium`` gdy
           ``moc_pattern = "MOC___{name}"``).

        Legacy prefix ``MOC__X`` (podwojne podkreslenie) NIE jest juz
        rozpoznawany — pliki tego typu muszly zostac zmigrowane do
        konwencji z configu przed wlaczeniem Fazy 7.
        """

        if (note.type or "").lower() == "moc":
            return True
        stem = Path(note.path).stem
        if stem.startswith(self._primary_prefix):
            return True
        return False

    def _moc_label(self, moc: VaultNote) -> str | None:
        """Wyciaga label MOC-a (np. ``Core`` z ``MOC___Core.md``).

        Parsuje stem wzgledem primary prefixu/suffixu z ``moc_pattern``.
        Zwraca ``None`` gdy stem nie pasuje do wzorca.
        """

        stem = Path(moc.path).stem
        if not stem.startswith(self._primary_prefix):
            return None
        label = stem[len(self._primary_prefix):]
        if self._primary_suffix and label.endswith(self._primary_suffix):
            label = label[: -len(self._primary_suffix)] if self._primary_suffix else label
        return label or None

    def _section_title_for(self, note: VaultNote) -> str:
        if self._is_moc_note(note):
            return _MOC_SECTION
        if note.type:
            return note.type.strip() or _OTHER_SECTION
        return _OTHER_SECTION

    @staticmethod
    def _render_initial_index(section: str, entry: str) -> str:
        return (
            "# Index vaulta\n\n"
            "Auto-utrzymywany przez `MOCManager`. Nie edytuj recznie sekcji ponizej —\n"
            "agent moze nadpisac zmiany przy nastepnym `update_index` / `rebuild_index`.\n\n"
            f"## {section}\n\n{entry}\n"
        )

    @classmethod
    def _entry_already_in_section(cls, raw: str, section: str, entry: str) -> bool:
        section_text = cls._extract_section_body(raw, section)
        if section_text is None:
            return False
        return cls._line_present(section_text, entry)

    @staticmethod
    def _line_present(text: str, entry: str) -> bool:
        target = entry.strip()
        for line in text.splitlines():
            if line.strip() == target:
                return True
        return False

    @classmethod
    def _extract_section_body(cls, raw: str, section: str) -> str | None:
        lines = raw.splitlines()
        start: int | None = None
        start_level: int | None = None

        for idx, line in enumerate(lines):
            match = _HEADING_LINE_RE.match(line)
            if not match:
                continue
            level = len(match.group(1))
            title = match.group(2).strip()
            if start is None and title == section:
                start = idx + 1
                start_level = level
                continue
            if start is not None and start_level is not None and level <= start_level:
                return "\n".join(lines[start:idx])

        if start is None:
            return None
        return "\n".join(lines[start:])

    @classmethod
    def _insert_entry_into_index(cls, raw: str, section: str, entry: str) -> str:
        lines = raw.splitlines()
        section_start: int | None = None
        section_level: int | None = None
        section_end: int | None = None

        for idx, line in enumerate(lines):
            match = _HEADING_LINE_RE.match(line)
            if not match:
                continue
            level = len(match.group(1))
            title = match.group(2).strip()
            if section_start is None and title == section:
                section_start = idx
                section_level = level
                continue
            if (
                section_start is not None
                and section_level is not None
                and level <= section_level
            ):
                section_end = idx
                break

        if section_start is None:
            tail = "" if raw.endswith("\n") else "\n"
            prefix = "" if raw.endswith("\n\n") or not raw else ("\n" if raw.endswith("\n") else "\n\n")
            return f"{raw}{prefix}## {section}\n\n{entry}\n"

        if section_end is None:
            section_end = len(lines)

        insertion = section_end
        while insertion > section_start + 1 and not lines[insertion - 1].strip():
            insertion -= 1

        new_lines = lines[:insertion] + [entry] + lines[insertion:]
        new_raw = "\n".join(new_lines)
        if raw.endswith("\n") and not new_raw.endswith("\n"):
            new_raw += "\n"
        return new_raw

    def _render_full_index(self, knowledge: VaultKnowledge, *, index_path: str) -> str:
        sections: dict[str, list[str]] = {}
        for note in sorted(knowledge.notes, key=lambda n: n.path):
            if note.path == index_path:
                continue
            section = self._section_title_for(note)
            sections.setdefault(section, []).append(f"- [[{Path(note.path).stem}]]")

        ordered_keys: list[str] = []
        if _MOC_SECTION in sections:
            ordered_keys.append(_MOC_SECTION)
        for key in sorted(k for k in sections if k not in {_MOC_SECTION, _OTHER_SECTION}):
            ordered_keys.append(key)
        if _OTHER_SECTION in sections:
            ordered_keys.append(_OTHER_SECTION)

        body_parts: list[str] = [
            "# Index vaulta\n",
            "Auto-utrzymywany przez `MOCManager.rebuild_index`. Wpisy generowane ze\n"
            "skanu vaulta — recznie edytowane sekcje moga zostac nadpisane.\n",
        ]
        for key in ordered_keys:
            entries = "\n".join(sections[key])
            body_parts.append(f"## {key}\n\n{entries}\n")

        return "\n".join(body_parts)


_BOOTSTRAP_TEXTS = {
    "pl": {
        "intro": (
            "> **Glowny index wiedzy o projekcie.** Auto-utworzony przez agenta "
            "przy pierwszym biegu — traktuj jako punkt startowy grafu wiedzy. "
            "Agent i user beda tutaj dopisywac linki do hubow, modulow, "
            "technologii i decyzji architektonicznych.\n\n"
            "> **Dla agenta AI:** Wszystkie notatki vaulta powinny miec ten plik "
            "jako docelowy `parent` (bezposrednio albo przez hub). Hierarchia: "
            "MOC → huby tematyczne → notatki szczegolowe."
        ),
        "hubs": "Huby",
        "hubs_placeholder": "_(pusto — dopisz linki do hubow tematycznych, np. `[[Architektura_systemu]]`)_",
        "modules": "Moduly",
        "modules_placeholder": "_(pusto — dopisz linki do modulow kodu, np. `[[Agent]]`)_",
        "technologies": "Technologie",
        "technologies_placeholder": "_(pusto — dopisz linki do notatek `type: technology`)_",
        "decisions": "Decyzje architektoniczne",
        "decisions_table_header": "| Decyzja | Status | Uzasadnienie |\n|---|---|---|",
        "decisions_placeholder": "_(pusto — `create_decision` bedzie dopisywac wiersze automatycznie)_",
        "concepts": "Koncepty",
        "concepts_placeholder": "_(pusto — dopisz linki do notatek `type: concept`)_",
        "glossary": "Slownik",
        "glossary_placeholder": "_(pusto — krotkie definicje pojec uzywanych wokol projektu)_",
    },
    "en": {
        "intro": (
            "> **Root knowledge index of the project.** Auto-created by the agent "
            "on the first run — treat as the graph entry point. Agent and user "
            "will append links to hubs, modules, technologies and ADRs here.\n\n"
            "> **For the AI agent:** All vault notes should point to this file as "
            "their `parent` (directly or via a hub). Hierarchy: "
            "MOC → topical hubs → detailed notes."
        ),
        "hubs": "Hubs",
        "hubs_placeholder": "_(empty — add links to topical hubs, e.g. `[[System_Architecture]]`)_",
        "modules": "Modules",
        "modules_placeholder": "_(empty — add links to code modules, e.g. `[[Agent]]`)_",
        "technologies": "Technologies",
        "technologies_placeholder": "_(empty — add links to `type: technology` notes)_",
        "decisions": "Architectural decisions",
        "decisions_table_header": "| Decision | Status | Rationale |\n|---|---|---|",
        "decisions_placeholder": "_(empty — `create_decision` will append rows automatically)_",
        "concepts": "Concepts",
        "concepts_placeholder": "_(empty — add links to `type: concept` notes)_",
        "glossary": "Glossary",
        "glossary_placeholder": "_(empty — short definitions of domain terms)_",
    },
}


def render_bootstrap_moc(
    *,
    name: str,
    title: str | None = None,
    language: str = "pl",
    created: date | None = None,
    sections: dict[str, list[str]] | None = None,
) -> str:
    """Renderuje zawartosc startowego MOC-a vaulta — deterministycznie.

    Wyjscie: frontmatter YAML (``type: moc``, ``tags: [moc, <name.lower()>]``,
    ``status: active``) + body z sekcjami Huby / Moduly / Technologie / Decyzje /
    Koncepty / Slownik.

    Jesli ``sections`` dostarczone i nie pusto dla danego klucza -> sekcja
    dostaje listy wikilinkow ``- [[stem]]``. W przeciwnym razie sekcja dostaje
    placeholder ``_(pusto — ...)_`` — zeby user widzial strukture i mial miejsce
    na reczne wpisy.

    :param name: label MOC-a (np. ``"Kompendium"``) — ladowany do tagu i tytulu.
    :param title: naglowek ``# ...``. ``None`` -> ``"MOC — {name}"``.
    :param language: ``"pl"`` / ``"en"``.
    :param created: data ``YYYY-MM-DD`` do pol ``created`` / ``updated``.
    :param sections: mapa ``{tytul_sekcji: [stem, stem, ...]}`` z wpisami do
        wypelnienia. Klucze musza odpowiadac tytulom w szablonie jezykowym
        (np. ``"Moduly"`` dla ``pl``). Nieznane klucze sa dopisywane jako
        dodatkowe sekcje na koncu (np. ``Inne`` / ``Other`` fallback).
    """

    if not isinstance(name, str) or not name.strip():
        raise ValueError("render_bootstrap_moc.name musi byc niepustym stringiem")
    label = name.strip()
    lang_key = language.strip().lower() if isinstance(language, str) else "pl"
    texts = _BOOTSTRAP_TEXTS.get(lang_key) or _BOOTSTRAP_TEXTS["pl"]
    sections = sections or {}

    today = (created or datetime.now(timezone.utc).date()).isoformat()
    heading = (title or f"MOC — {label}").strip()

    extra_tag = label.lower()
    tag_line = "  - moc" if extra_tag == "moc" else f"  - moc\n  - {extra_tag}"

    frontmatter = (
        "---\n"
        "type: moc\n"
        "tags:\n"
        f"{tag_line}\n"
        "status: active\n"
        f"created: {today}\n"
        f"updated: {today}\n"
        "---\n"
    )

    # kanoniczna kolejnosc sekcji + placeholder dla pustych
    canonical: list[tuple[str, str, bool]] = [
        (texts["hubs"], texts["hubs_placeholder"], False),
        (texts["modules"], texts["modules_placeholder"], False),
        (texts["technologies"], texts["technologies_placeholder"], False),
        (
            texts["decisions"],
            f"{texts['decisions_table_header']}\n\n{texts['decisions_placeholder']}",
            True,  # ta sekcja zawsze dostaje tabelke, niezaleznie od wpisow
        ),
        (texts["concepts"], texts["concepts_placeholder"], False),
        (texts["glossary"], texts["glossary_placeholder"], False),
    ]

    body_parts: list[str] = [f"# {heading}", "", texts["intro"], ""]
    canonical_keys = {entry[0] for entry in canonical}

    def _render_section(section_title: str, placeholder: str, has_table: bool) -> None:
        body_parts.append(f"## {section_title}")
        body_parts.append("")
        stems = sections.get(section_title) or []
        if has_table:
            body_parts.append(texts["decisions_table_header"])
            body_parts.append("")
            if stems:
                for stem in stems:
                    body_parts.append(f"- [[{stem}]]")
            else:
                body_parts.append(texts["decisions_placeholder"])
        elif stems:
            for stem in stems:
                body_parts.append(f"- [[{stem}]]")
        else:
            body_parts.append(placeholder)
        body_parts.append("")

    for section_title, placeholder, has_table in canonical:
        _render_section(section_title, placeholder, has_table)

    # dodatkowe sekcje spoza kanonu (np. fallback "Inne" / "Other")
    extras = [k for k in sections.keys() if k not in canonical_keys]
    for section_title in sorted(extras):
        stems = sections[section_title]
        if not stems:
            continue
        body_parts.append(f"## {section_title}")
        body_parts.append("")
        for stem in stems:
            body_parts.append(f"- [[{stem}]]")
        body_parts.append("")

    body = "\n".join(body_parts).rstrip() + "\n"
    return frontmatter + "\n" + body
