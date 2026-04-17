"""Persystentny cache chunk\u00f3w diffa i ich podsumowan AI.

Trzymany na dysku obok ``.agent-state.json`` (w folderze ``config.yaml``).
Motywacja: przy multi-turn chunk-summary placimy za kazde zapytanie do
AI. Gdy user ubije proces w polowie (Ctrl+C), zrobi retry walidacji AI
na FINALIZE albo wroci do tego samego commita po kilku dniach \u2014
nie chcemy drugi raz prosic AI o te same summaries.

**Klucz cache**: ``(commit_sha, chunk_idx, total_chunks)``.
Gitowy sha gwarantuje, ze zawartosc chunka jest identyczna (immutable
historia). ``chunk_idx`` + ``total_chunks`` rozrozniaja chunki w obrebie
commita. Bez ``file_path`` \u2014 chunk moze zawierac fragmenty wielu
plikow, wiec path nie ma sensu jako klucz.

**Co cachujemy:**

- ``chunks/``    \u2014 sam tekst chunka (tak zeby debug / replay byl tani)
- ``summaries/`` \u2014 ``ChunkSummary`` z textem od AI + metadanymi

Format plikow: JSON (czytelny dla czlowieka, latwy do inspekcji).
Atomowosc zapisu: os.replace z pliku ``.tmp`` obok docelowego (ten
sam wzorzec co ``AgentStateStore``).

Cache **nie ma** ewikcji \u2014 rosnie razem z historia projektu. W
praktyce dla normalnego repo to max kilka MB miesiecznie. Usuwanie
reczne przez ``rm -rf .agent-cache/`` jest bezpieczne (chunki zostana
odtworzone przy nastepnym biegu, summaries odliczone ponownie).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from src.agent.models_chunks import ChunkCacheKey, ChunkSummary, DiffChunk

logger = logging.getLogger(__name__)


DEFAULT_CACHE_DIRNAME = ".agent-cache"
_CHUNKS_SUBDIR = "chunks"
_SUMMARIES_SUBDIR = "summaries"


class ChunkCache:
    """Czyta i zapisuje chunki + podsumowania AI w ``.agent-cache/``.

    Jedna instancja per bieg agenta. Nie jest thread-safe, ale agent
    jest single-threaded (petla w ``main.py`` iteruje sekwencyjnie).

    Typowe uzycie:

    >>> cache = ChunkCache.from_config("config.yaml")
    >>> if (summary := cache.get_summary(sha, chunk)) is not None:
    ...     # skip AI call
    ... else:
    ...     summary = await provider.complete(...)
    ...     cache.put_summary(sha, chunk, summary)
    """

    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir).expanduser().resolve()
        self.chunks_dir = self.cache_dir / _CHUNKS_SUBDIR
        self.summaries_dir = self.cache_dir / _SUMMARIES_SUBDIR

    @classmethod
    def from_config(cls, config_path: str | Path) -> "ChunkCache":
        """Buduje cache dir \u2014 domyslnie ``<obok config.yaml>/.agent-cache/``.

        Nie czyta nic z samego configu (jeszcze) \u2014 sciezka jest
        pochodna config_path. Gdy kiedys dodamy ``agent.cache_dir``,
        wystarczy zmienic tutaj jednen if.
        """

        cfg_path = Path(config_path).expanduser().resolve()
        cache_dir = cfg_path.parent / DEFAULT_CACHE_DIRNAME
        return cls(cache_dir=cache_dir)

    def get_chunk(self, commit_sha: str, chunk: DiffChunk) -> DiffChunk | None:
        """Odczyt chunka z cache. Zwraca ``None`` gdy brak lub uszkodzony."""

        path = self._chunk_path(commit_sha, chunk)
        if not path.is_file():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return DiffChunk.model_validate(data)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            logger.warning("Cache chunk uszkodzony %s: %s \u2014 ignoruje.", path, exc)
            return None

    def put_chunk(self, commit_sha: str, chunk: DiffChunk) -> None:
        """Zapisuje chunk do cache (atomowo)."""

        path = self._chunk_path(commit_sha, chunk)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = chunk.model_dump_json(indent=2)
        self._atomic_write(path, payload)

    def get_summary(self, commit_sha: str, chunk: DiffChunk) -> ChunkSummary | None:
        """Odczyt podsumowania AI z cache. ``None`` = nie bylo albo zepsute."""

        path = self._summary_path(commit_sha, chunk)
        if not path.is_file():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return ChunkSummary.model_validate(data)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            logger.warning("Cache summary uszkodzony %s: %s \u2014 ignoruje.", path, exc)
            return None

    def put_summary(self, commit_sha: str, chunk: DiffChunk, summary: ChunkSummary) -> None:
        """Zapisuje podsumowanie AI atomowo."""

        path = self._summary_path(commit_sha, chunk)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = summary.model_dump_json(indent=2)
        self._atomic_write(path, payload)

    def invalidate_commit(self, commit_sha: str) -> int:
        """Usuwa cache dla calego commita (chunki + summaries).

        Zwraca liczbe usunietych plikow. Uzywana przy retry walidacji
        AI na FINALIZE, gdy user chce zaczac od zera (not implemented
        in flow yet, ale udostepnione dla elastycznosci).
        """

        count = 0
        key_dir_name = commit_sha[:12]
        for sub in (self.chunks_dir, self.summaries_dir):
            target = sub / key_dir_name
            if not target.is_dir():
                continue
            for entry in target.iterdir():
                try:
                    entry.unlink()
                    count += 1
                except OSError:
                    logger.warning("Nie udalo sie usunac cache %s", entry)
            try:
                target.rmdir()
            except OSError:
                pass
        return count

    def _chunk_path(self, commit_sha: str, chunk: DiffChunk) -> Path:
        key = ChunkCacheKey(commit_sha=commit_sha, chunk=chunk)
        return self.chunks_dir / key.dir_name() / key.filename(suffix=".json")

    def _summary_path(self, commit_sha: str, chunk: DiffChunk) -> Path:
        key = ChunkCacheKey(commit_sha=commit_sha, chunk=chunk)
        return self.summaries_dir / key.dir_name() / key.filename(suffix=".json")

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        """Zapisuje plik atomowo: tmp-file obok + os.replace.

        Duplikuje wzorzec z ``AgentStateStore.save`` \u2014 niestety w stdlib
        Pythona nie ma publicznego helpera, a wzorzec jest dokladnie
        ten sam (Ctrl+C w trakcie write nie zepsuje starego pliku).
        """

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    logger.warning("Nie udalo sie usunac %s po bledzie zapisu.", tmp_path)
            raise


__all__ = ["DEFAULT_CACHE_DIRNAME", "ChunkCache"]
