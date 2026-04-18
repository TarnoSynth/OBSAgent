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
