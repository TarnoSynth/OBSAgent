"""Modele chunkingu diffow dla konsumpcji AI.

**Filozofia chunkingu** (potwierdzona decyzja user):

1. Bierzemy **caly diff commita** \u2014 wszystkie hunki ze wszystkich
   plikow (po filtrze ``ignore_patterns``).
2. Dopiero POTEM tniemy na chunki po ``max_diff_lines``.
3. Jeden chunk moze zawierac hunki z **wielu plikow** (gdy pliki sa
   male \u2014 grupujemy je razem zeby nie placic za N osobnych requestow).
4. Jeden chunk moze tez zawierac tylko kawalek jednego hunka (gdy
   pojedynczy hunk > ``max_diff_lines``, wybor user ``big_hunk=split_lines``).

Cache key: ``(commit_sha, chunk_idx, total_chunks)`` \u2014 bez path, bo
chunk jest "global" w obrebie commita. Zawartosc chunka jest
deterministyczna dla danego sha + algorytmu dzielenia, wiec ta trojka
identyfikuje unikalnie.

Modele sa Pydanticowe \u2014 sa zapisywane do cache JSON. Pomiedzy biegami
tego samego commita moga zostac zdeserializowane bezposrednio.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from src.git.models import ChangeType, CommitInfo, CommitStats


def _short_hash(text: str, *, length: int = 10) -> str:
    """Stabilny krotki hash SHA-1 \u2014 uzywany jako chunk_id i split_group.

    SHA-1 bo: szybki, deterministyczny, nie potrzebujemy cryptograficznie
    mocnego hashowania (chunki nie sa sekretami). 10 znakow hex daje ~40 bitow,
    kolizja dla pojedynczego commita praktycznie niemozliwa.
    """

    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


ChunkKind = Literal["full_hunks", "split_hunk"]
"""Rodzaj chunka wg. strategii dzielenia:

- ``full_hunks`` \u2014 chunk zawiera jeden lub wiecej CALYCH hunkow
  (moze z wielu plikow). To standard.
- ``split_hunk``  \u2014 chunk zawiera CZESC jednego hunka, ktory byl
  wiekszy niz ``max_diff_lines`` i zostal pociety po liniach
  (decyzja user ``big_hunk=split_lines``). Split-hunki zawsze dotycza
  jednego pliku \u2014 jeden hunk nie da sie rozbic miedzy pliki.
"""


class DiffChunk(BaseModel):
    """Pojedynczy chunk diffa gotowy do wyslania do AI.

    Chunk moze zawierac fragmenty **wielu plikow** \u2014 gdy pliki sa male
    i laczymy je do budzetu ``max_diff_lines``. Dzieki temu nie placimy
    za 10 osobnych requestow przy commicie rozproszonym po 10 plikach
    po 20 linii.

    ``diff_text`` to kompletnie wyrenderowany fragment: dla kazdego
    pliku w chunku ma header ``diff --git`` + ``---``/``+++``, potem
    wszystkie jego hunki obecne w tym chunku. Rendering jest uporzadkowany
    wg kolejnosci plikow z commita (stabilny dla cache).

    **Klucz cache**: ``(commit_sha, chunk_idx, total_chunks)``.
    """

    chunk_idx: int = Field(..., ge=1)
    """1-based indeks chunka w obrebie commita."""

    total_chunks: int = Field(..., ge=1)
    """Ile lacznie chunkow ma commit."""

    diff_text: str
    """Wyrenderowany tekst chunka (headery plikow + hunki). Niepusty."""

    kind: ChunkKind = "full_hunks"

    file_paths: list[str] = Field(default_factory=list)
    """Sciezki plikow obecnych w tym chunku. Do preview / UI / logow.
    AI widzi je rowniez w diff_text przez ``diff --git`` headery."""

    hunk_count: int = Field(..., ge=0)
    """Ile hunkow znajduje sie w tym chunku (sumaryczne, wszystkie pliki)."""

    line_count: int = Field(..., ge=0)
    """Liczba linii w ``diff_text`` (cache'owane dla logow/metryki)."""

    chunk_id: str = Field(default="", min_length=0)
    """Krotkie, deterministyczne ID chunka (10 znakow hex, SHA-1 z ``diff_text``).

    Wypelniane automatycznie przez walidator przy tworzeniu. Unikalne
    dla kazdej tresci \u2014 dwa chunki o identycznym ``diff_text`` dostana
    ten sam ``chunk_id`` (nie przeszkadza, bo cache i tak keyuje przez
    commit_sha). Uzywane w promptach do AI jako stabilny odnosnik."""

    split_group: str | None = None
    """Tylko dla ``kind=split_hunk``: ID oryginalnego (niepodzielonego) hunka.

    Wszystkie czesci tego samego splitu maja IDENTYCZNY ``split_group``
    \u2014 dzieki temu AI widzi: "chunk a3f9c1d2b4 i chunk b7e2f04c1a to
    kawalki tego samego logicznego hunka xy789abc0e". Dla ``full_hunks``
    zawsze ``None``."""

    split_part: int | None = Field(default=None, ge=1)
    """Pozycja w splicie (1-based). Tylko dla ``kind=split_hunk``."""

    split_total: int | None = Field(default=None, ge=1)
    """Lacznie czesci w tym splicie. Tylko dla ``kind=split_hunk``."""

    @field_validator("diff_text")
    @classmethod
    def _validate_diff_text(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("diff_text nie moze byc pusty")
        return value

    @model_validator(mode="after")
    def _fill_and_validate_split_meta(self) -> "DiffChunk":
        if not self.chunk_id:
            object.__setattr__(self, "chunk_id", _short_hash(self.diff_text))

        if self.kind == "split_hunk":
            missing = [
                name for name, val in (
                    ("split_group", self.split_group),
                    ("split_part", self.split_part),
                    ("split_total", self.split_total),
                ) if val is None
            ]
            if missing:
                raise ValueError(
                    f"DiffChunk kind=split_hunk wymaga wypelnionych pol: {missing}"
                )
            if self.split_part > self.split_total:  # type: ignore[operator]
                raise ValueError(
                    f"split_part ({self.split_part}) nie moze byc > "
                    f"split_total ({self.split_total})"
                )
        else:
            extra = [
                name for name, val in (
                    ("split_group", self.split_group),
                    ("split_part", self.split_part),
                    ("split_total", self.split_total),
                ) if val is not None
            ]
            if extra:
                raise ValueError(
                    f"DiffChunk kind=full_hunks nie moze miec pol splitu: {extra}"
                )
        return self

    def cache_stem(self) -> str:
        """Stabilny stem do keya cache \u2014 ``chunk_<idx>of<total>``."""

        return f"chunk_{self.chunk_idx}of{self.total_chunks}"

    @property
    def file_count(self) -> int:
        return len(self.file_paths)

    @property
    def is_split(self) -> bool:
        return self.kind == "split_hunk"


class ChunkSummary(BaseModel):
    """Podsumowanie chunka wygenerowane przez AI (tryb multi-turn).

    Zapisywane w cache po pierwszym zapytaniu o ten chunk. Przy retry
    walidacji AI lub ponownym biegu po Ctrl+C \u2014 czytamy z cache
    zamiast placic ponownie za to samo.

    Keyowane przez ``chunk_idx`` w obrebie commita \u2014 bez ``file_path``,
    bo chunk moze zawierac wiele plikow.
    """

    chunk_idx: int
    total_chunks: int
    summary: str = Field(..., min_length=1)
    model: str = ""
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    file_paths: list[str] = Field(default_factory=list)
    """Sciezki plikow z chunka w momencie generowania podsumowania.
    Trzymane jako meta \u2014 nie wplywa na klucz cache, ale pomaga w
    czytaniu zdumpowanych plikow JSON i sanity-check po fakcie."""

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("summary nie moze byc tylko bialymi znakami")
        return stripped


class ChunkedCommit(BaseModel):
    """Commit projektowy przygotowany pod konsumpcje AI.

    Trzyma **lekki** ``CommitInfo`` (metadane + lista ``FileChange`` BEZ
    ``diff_text``) plus liste chunkow calego commita. Chunki sa
    globalnym podzialem wszystkich hunkow, nie per-plik.

    ``is_small`` to heurystyka czy commit da sie obsluzyc jednym requestem.
    Gdy ``total_chunks == 1`` \u2014 szybka sciezka (jeden request z
    ``submit_plan``). Powyzej \u2014 multi-turn z chunk-summary + FINALIZE.
    """

    commit: CommitInfo
    """Lekki CommitInfo \u2014 ``changes`` moga miec ``diff_text=""``."""

    chunks: list[DiffChunk] = Field(default_factory=list)
    """Chunki calego commita (nie per plik). Chunk 1 dotyczy hunkow
    z poczatku calej sekwencji diffow, chunk N z konca."""

    skipped_files: list[str] = Field(default_factory=list)
    """Sciezki plikow pominietych na poziomie ``ignore_patterns``."""

    @property
    def total_chunks(self) -> int:
        return len(self.chunks)

    @property
    def total_lines(self) -> int:
        return sum(chunk.line_count for chunk in self.chunks)

    def is_small(self, *, small_threshold_chunks: int = 1) -> bool:
        """``total_chunks <= small_threshold_chunks`` \u2192 sciezka 1-request."""

        return self.total_chunks <= small_threshold_chunks


class ChunkCacheKey(BaseModel):
    """Klucz cache'owy dla jednego chunka.

    Keyowany przez ``(commit_sha, chunk_idx, total_chunks)`` \u2014 bez
    ``file_path``, bo chunk moze zawierac wiele plikow.
    """

    commit_sha: str = Field(..., min_length=7)
    chunk: DiffChunk

    def filename(self, *, suffix: str) -> str:
        if not suffix.startswith("."):
            suffix = "." + suffix
        return f"{self.chunk.cache_stem()}{suffix}"

    def dir_name(self) -> str:
        """Nazwa folderu (sha skrocone do 12 znakow)."""

        return self.commit_sha[:12]


def default_line_count(text: str) -> int:
    """Liczy linie w tekscie diffa (helper dla testow i konstrukcji DiffChunk)."""

    if not text:
        return 0
    return len(text.splitlines())


def posix_path(path: str) -> str:
    """Normalizuje separator sciezki do POSIX (``/``) dla spojnosci."""

    return str(PurePosixPath(path.replace("\\", "/")))


__all__ = [
    "ChangeType",
    "ChunkCacheKey",
    "ChunkKind",
    "ChunkSummary",
    "ChunkedCommit",
    "CommitInfo",
    "CommitStats",
    "DiffChunk",
    "default_line_count",
    "posix_path",
    "_short_hash",
]
