"""VaultManager — czysta warstwa semantyki Obsidiana.

Kontrakt warstwy:

- **NIE** zna Gita (nie pullu, nie commituje, nie sprawdza historii)
- **NIE** zna AI ani ``CommitInfo`` z warstwy git
- **NIE** decyduje co i gdzie linkowac (MOC, indeksy) — to warstwa agenta
- Dziala na dowolnym folderze z plikami ``.md``, nawet jesli to nie jest repo

Co robi:

- Parsuje frontmatter YAML, wikilinki ``[[...]]``, tagi ``#tag`` i tytul ``# ...``
- Skanuje caly vault do ``VaultKnowledge`` (mapa aktualnego stanu)
- Wykonuje prymitywne operacje na plikach: ``create`` / ``overwrite`` / ``append`` / ``delete``
- Serializuje ``VaultNote`` z powrotem do pliku .md

Synchronizacje z remote robi ``src.git.GitSyncer`` — to zadanie agenta, nie vaulta.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from src.vault.models import VaultKnowledge, VaultNote

logger = logging.getLogger(__name__)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_TAG_RE = re.compile(r"(?<![\w/])#([\w-]+)")
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")
_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

_SKIPPED_DIRS = {".obsidian", ".git", ".trash"}


class VaultManager:
    """Warstwa semantyki Obsidiana — CRUD + parsowanie .md.

    Nie zna Gita ani AI. Operuje wylacznie na aktualnym stanie plikow w folderze.
    Wszystkie decyzje (co linkowac, jak aktualizowac MOC) nalezy do agenta.
    """

    def __init__(self, vault_path: str | Path) -> None:
        self.vault_path = Path(vault_path).expanduser().resolve()
        if not self.vault_path.is_dir():
            raise ValueError(f"Vault {self.vault_path} nie istnieje lub nie jest katalogiem.")

    @classmethod
    def from_config(cls, config_path: str | Path) -> "VaultManager":
        """Buduje ``VaultManager`` na podstawie ``config.yaml``."""

        from src.providers import load_config_dict

        cfg = load_config_dict(config_path)
        paths = cfg.get("paths")
        if not isinstance(paths, dict):
            raise ValueError("config: sekcja 'paths' musi byc mapa")

        vault = paths.get("vault")
        if not vault or not isinstance(vault, str):
            raise ValueError("config: paths.vault jest wymagane")

        return cls(vault_path=vault)

    def scan_all(self) -> VaultKnowledge:
        """Skanuje caly vault i buduje ``VaultKnowledge`` — search index dla coding assistanta.

        Indeksy (``by_type`` / ``by_tag`` / ``by_status`` / ``children_index`` /
        ``backlinks_index`` / ``related_index``) sa budowane w jednym przejsciu
        i umozliwiaja O(1) zapytania typu "wszystkie ADR-y", "co linkuje do Auth",
        "co jest dzieckiem MOC__Core".

        Osierocone wikilinki liczone sa w aktualnym stanie plikow — nie uwzgledniaja
        historii Gita.
        """

        notes: list[VaultNote] = []
        for md_path in self._iter_markdown_files():
            try:
                note = self._parse_note_from_path(md_path)
            except Exception:
                logger.exception("Pominieto notatke %s — blad parsowania.", md_path)
                continue
            notes.append(note)

        notes.sort(key=lambda n: n.path)

        by_path: dict[str, VaultNote] = {note.path: note for note in notes}
        by_stem: dict[str, list[str]] = {}
        by_type: dict[str, list[str]] = {}
        by_tag: dict[str, list[str]] = {}
        by_status: dict[str, list[str]] = {}
        children_index: dict[str, list[str]] = {}
        backlinks_index: dict[str, list[str]] = {}
        related_index: dict[str, list[str]] = {}

        all_wikilinks: set[str] = set()
        all_tags: set[str] = set()
        moc_files: list[str] = []

        for note in notes:
            all_wikilinks.update(note.wikilinks)
            all_tags.update(note.tags)
            if Path(note.path).name.startswith("MOC__"):
                moc_files.append(note.path)

            by_stem.setdefault(Path(note.path).stem, []).append(note.path)
            if note.type:
                by_type.setdefault(note.type, []).append(note.path)
            for tag in note.tags:
                by_tag.setdefault(tag, []).append(note.path)
            if note.status:
                by_status.setdefault(note.status, []).append(note.path)
            if note.parent:
                children_index.setdefault(note.parent, []).append(note.path)
            for link in note.wikilinks:
                backlinks_index.setdefault(link, []).append(note.path)
            for rel in note.related:
                related_index.setdefault(rel, []).append(note.path)

        existing_basenames = set(by_stem.keys())
        existing_stem_paths = {Path(n.path).with_suffix("").as_posix() for n in notes}
        orphaned_links = sorted(
            link
            for link in all_wikilinks
            if link not in existing_basenames and link not in existing_stem_paths
        )
        moc_files.sort()

        return VaultKnowledge(
            total_notes=len(notes),
            notes=notes,
            all_tags=all_tags,
            all_wikilinks=all_wikilinks,
            moc_files=moc_files,
            orphaned_links=orphaned_links,
            by_path=by_path,
            by_stem=by_stem,
            by_type=by_type,
            by_tag=by_tag,
            by_status=by_status,
            children_index=children_index,
            backlinks_index=backlinks_index,
            related_index=related_index,
        )

    def note_exists(self, rel_path: str) -> bool:
        """Sprawdza czy notatka istnieje pod podana sciezka wzgledna."""

        return self._resolve_safe_path(rel_path).is_file()

    def read_note(self, rel_path: str) -> VaultNote:
        """Czyta pojedyncza notatke z parsowaniem frontmattera, tagow i wikilinkow."""

        abs_path = self._resolve_safe_path(rel_path)
        if not abs_path.is_file():
            raise FileNotFoundError(f"Notatka {rel_path!r} nie istnieje w vaulcie.")
        return self._parse_note_from_path(abs_path)

    def write_note(self, note: VaultNote) -> None:
        """Zapisuje ``VaultNote`` (serializuje frontmatter, nadpisuje plik jesli istnieje)."""

        abs_path = self._resolve_safe_path(note.path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(self._serialize_note(note), encoding="utf-8")
        logger.debug("Zapisano notatke: %s", note.path)

    def create(self, rel_path: str, content: str) -> None:
        """Tworzy nowy plik .md z surowa zawartoscia. Rzuca ``FileExistsError``, jesli juz istnieje."""

        abs_path = self._resolve_safe_path(rel_path)
        if abs_path.exists():
            raise FileExistsError(
                f"Create: plik {rel_path!r} juz istnieje — uzyj 'overwrite' albo 'append'."
            )
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")
        logger.debug("Create: %s", rel_path)

    def overwrite(self, rel_path: str, content: str) -> None:
        """Nadpisuje istniejacy plik .md surowa zawartoscia. Rzuca ``FileNotFoundError``, jesli nie istnieje."""

        abs_path = self._resolve_safe_path(rel_path)
        if not abs_path.is_file():
            raise FileNotFoundError(
                f"Overwrite: plik {rel_path!r} nie istnieje — uzyj 'create'."
            )
        abs_path.write_text(content, encoding="utf-8")
        logger.debug("Overwrite: %s", rel_path)

    def append(self, rel_path: str, content: str) -> None:
        """Dopisuje tresc do istniejacego pliku .md. Rzuca ``FileNotFoundError``, jesli nie istnieje."""

        abs_path = self._resolve_safe_path(rel_path)
        if not abs_path.is_file():
            raise FileNotFoundError(
                f"Append: plik {rel_path!r} nie istnieje — uzyj 'create'."
            )
        existing = abs_path.read_text(encoding="utf-8")
        separator = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        with abs_path.open("a", encoding="utf-8") as f:
            f.write(separator + content)
        logger.debug("Append: %s", rel_path)

    def delete(self, rel_path: str) -> None:
        """Usuwa plik .md. Rzuca ``FileNotFoundError``, jesli nie istnieje."""

        abs_path = self._resolve_safe_path(rel_path)
        if not abs_path.is_file():
            raise FileNotFoundError(f"Delete: plik {rel_path!r} nie istnieje.")
        abs_path.unlink()
        logger.debug("Delete: %s", rel_path)

    def read_text(self, rel_path: str) -> str:
        """Surowy odczyt pliku .md (bez parsowania frontmattera).

        Uzywany przez ``MOCManager`` do operowania na MOC/_index, gdzie liczy sie
        zachowanie dokladnej tresci pliku (sekcje markdown, formatowanie).
        """

        abs_path = self._resolve_safe_path(rel_path)
        if not abs_path.is_file():
            raise FileNotFoundError(f"read_text: plik {rel_path!r} nie istnieje.")
        return abs_path.read_text(encoding="utf-8")

    def write_text(self, rel_path: str, content: str) -> None:
        """Surowy zapis pliku .md — tworzy lub nadpisuje (idempotentny prymityw).

        W przeciwienstwie do ``create`` / ``overwrite`` nie wymaga znajomosci
        czy plik istnieje — uzyteczne dla skladowych ktore aktualizuja MOC /
        ``_index.md`` niezaleznie od stanu poczatkowego.
        """

        abs_path = self._resolve_safe_path(rel_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")
        logger.debug("write_text: %s", rel_path)

    def _iter_markdown_files(self) -> Iterable[Path]:
        for path in self.vault_path.rglob("*.md"):
            if not path.is_file():
                continue
            if self._should_skip_path(path):
                continue
            yield path

    def _should_skip_path(self, path: Path) -> bool:
        try:
            rel_parts = path.relative_to(self.vault_path).parts
        except ValueError:
            return True
        for part in rel_parts[:-1]:
            if part in _SKIPPED_DIRS or part.startswith("."):
                return True
        return False

    def _parse_note_from_path(self, abs_path: Path) -> VaultNote:
        raw = abs_path.read_text(encoding="utf-8")
        frontmatter, body = self._extract_frontmatter(raw)

        tags_from_fm = self._tags_from_frontmatter(frontmatter)
        tags_from_body = self._extract_tags(body)
        tags = sorted(set(tags_from_fm) | set(tags_from_body))

        wikilinks = self._extract_wikilinks(body)
        title = self._extract_title(body) or abs_path.stem
        rel_path = abs_path.relative_to(self.vault_path).as_posix()

        type_ = self._coerce_str(frontmatter.get("type"))
        parent = self._coerce_wikilink(frontmatter.get("parent"))
        related = self._coerce_wikilink_list(frontmatter.get("related"))
        status = self._coerce_str(frontmatter.get("status"))
        created = self._coerce_datetime(frontmatter.get("created"))
        updated = self._coerce_datetime(frontmatter.get("updated"))

        modified = self._coerce_datetime(frontmatter.get("modified"))
        if modified is None:
            modified = datetime.fromtimestamp(abs_path.stat().st_mtime)

        return VaultNote(
            path=rel_path,
            title=title,
            content=body,
            frontmatter=frontmatter,
            tags=tags,
            type=type_,
            parent=parent,
            related=related,
            status=status,
            created=created,
            updated=updated,
            modified=modified,
            wikilinks=wikilinks,
        )

    @staticmethod
    def _extract_frontmatter(raw: str) -> tuple[dict, str]:
        match = _FRONTMATTER_RE.match(raw)
        if not match:
            return {}, raw

        yaml_text = match.group(1)
        try:
            parsed = yaml.safe_load(yaml_text)
        except yaml.YAMLError:
            logger.warning("Bledny YAML frontmatter — zwracam pusty dict.")
            parsed = None

        body = raw[match.end():]
        if not isinstance(parsed, dict):
            return {}, body
        return parsed, body

    @staticmethod
    def _tags_from_frontmatter(frontmatter: dict) -> list[str]:
        raw_tags: Any = frontmatter.get("tags") if frontmatter else None
        if raw_tags is None:
            return []
        if isinstance(raw_tags, str):
            candidates = re.split(r"[,\s]+", raw_tags)
        elif isinstance(raw_tags, Iterable):
            candidates = [str(item) for item in raw_tags]
        else:
            return []
        return [tag.lstrip("#").strip() for tag in candidates if str(tag).strip()]

    @staticmethod
    def _coerce_str(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def _coerce_wikilink(cls, value: Any) -> str | None:
        """Zwraca sam target wikilinka bez nawiasow kwadratowych.

        Przyklady:
            ``"[[MOC__Core]]"`` -> ``"MOC__Core"``
            ``"[[Auth|autoryzacja]]"`` -> ``"Auth"``
            ``"[[Auth#Section]]"`` -> ``"Auth"``
            ``"Plain text"`` -> ``"Plain text"``
        """

        text = cls._coerce_str(value)
        if text is None:
            return None
        match = _WIKILINK_RE.match(text)
        if not match:
            return text
        target = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
        return target or text

    @classmethod
    def _coerce_wikilink_list(cls, value: Any) -> list[str]:
        """Lista wikilinkow (np. ``related: ["[[Auth]]", "[[Infra]]"]``) — zwraca targety."""

        if value is None:
            return []
        if isinstance(value, str):
            items: list[Any] = [value]
        elif isinstance(value, Iterable):
            items = list(value)
        else:
            return []

        result: list[str] = []
        seen: set[str] = set()
        for item in items:
            target = cls._coerce_wikilink(item)
            if target and target not in seen:
                seen.add(target)
                result.append(target)
        return result

    @staticmethod
    def _coerce_datetime(value: Any) -> datetime | None:
        """Akceptuje ``datetime``, ``date`` i stringi ISO-8601 (YYYY-MM-DD / pelna data-godzina)."""

        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                logger.warning("Niepoprawny format daty we frontmatterze: %r — pomijam.", text)
                return None
        logger.warning("Nieobslugiwany typ daty we frontmatterze: %r — pomijam.", type(value).__name__)
        return None

    @classmethod
    def _extract_tags(cls, body: str) -> list[str]:
        stripped = _FENCED_CODE_RE.sub(" ", body)
        stripped = _INLINE_CODE_RE.sub(" ", stripped)
        stripped = _MD_LINK_RE.sub(" ", stripped)
        stripped = _WIKILINK_RE.sub(" ", stripped)

        tags: list[str] = []
        seen: set[str] = set()
        for match in _TAG_RE.finditer(stripped):
            tag = match.group(1)
            if tag in seen:
                continue
            seen.add(tag)
            tags.append(tag)
        return tags

    @classmethod
    def _extract_wikilinks(cls, body: str) -> list[str]:
        stripped = _FENCED_CODE_RE.sub(" ", body)
        stripped = _INLINE_CODE_RE.sub(" ", stripped)

        links: list[str] = []
        seen: set[str] = set()
        for match in _WIKILINK_RE.finditer(stripped):
            raw = match.group(1).strip()
            target = raw.split("|", 1)[0].split("#", 1)[0].strip()
            if not target or target in seen:
                continue
            seen.add(target)
            links.append(target)
        return links

    @staticmethod
    def _extract_title(body: str) -> str | None:
        match = _HEADING_RE.search(body)
        if not match:
            return None
        return match.group(1).strip()

    def _serialize_note(self, note: VaultNote) -> str:
        if note.frontmatter:
            yaml_text = yaml.safe_dump(
                note.frontmatter, allow_unicode=True, sort_keys=False
            ).rstrip("\n")
            return f"---\n{yaml_text}\n---\n{note.content}"
        return note.content

    def _resolve_safe_path(self, rel_path: str) -> Path:
        if not rel_path or not isinstance(rel_path, str):
            raise ValueError(f"Nieprawidlowa sciezka notatki: {rel_path!r}")

        candidate = Path(rel_path)
        if candidate.is_absolute() or candidate.drive:
            raise ValueError(f"Sciezka {rel_path!r} musi byc wzgledna wzgledem vaulta.")
        if any(part == ".." for part in candidate.parts):
            raise ValueError(f"Sciezka {rel_path!r} nie moze wychodzic poza vault ('..').")

        target = (self.vault_path / candidate).resolve()
        try:
            target.relative_to(self.vault_path)
        except ValueError as exc:
            raise ValueError(
                f"Sciezka {rel_path!r} wskazuje poza vault {self.vault_path}."
            ) from exc
        return target
