"""Parser diff\u00f3w Gita i chunker **calego commita** (nie per plik).

Filozofia (potwierdzona decyzja user):

1. Zbieramy wszystkie hunki ze wszystkich plikow commita w jedna
   plaska liste z meta "z ktorego pliku pochodzi".
2. Tniemy ta liste na chunki po ``max_diff_lines`` linii.
3. Chunk moze zawierac hunki z wielu plikow \u2014 gdy sa male.
4. Jeden hunk > ``max_diff_lines`` jest splitowany po liniach (decyzja
   user ``big_hunk=split_lines``); splity nigdy nie sa mieszane z
   innymi plikami \u2014 kazdy split to osobny chunk ``kind=split_hunk``.

### Format diffa Gita

```
diff --git a/src/foo.py b/src/foo.py
index abc1234..def5678 100644
--- a/src/foo.py
+++ b/src/foo.py
@@ -10,5 +10,7 @@ def foo():
     ...
```

- **file header** (``diff --git``, ``index``, ``---``, ``+++``)
  \u2014 kazda sekcja pliku w chunku musi go zawierac.
- **hunki** (``@@ -A,B +C,D @@``) \u2014 logiczne jednostki zmiany.

### Renderowanie chunka z wielu plikow

Gdy chunk zawiera hunki z plikow A i B, diff_text wyglada tak:

```
diff --git a/A b/A
index ...
--- a/A
+++ b/A
@@ hunk1 @@
...
@@ hunk2 @@
...

diff --git a/B b/B
index ...
--- a/B
+++ b/B
@@ hunk3 @@
...
```

Pusta linia miedzy sekcjami plikow poprawia czytelnosc dla AI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.agent.models_chunks import ChunkKind, DiffChunk, _short_hash, default_line_count
from src.git.models import FileChange


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Hunk:
    """Wewnetrzna reprezentacja jednego hunka."""

    header: str
    """Pierwsza linia hunka: ``@@ -10,5 +10,7 @@ def foo():``"""

    body_lines: list[str]
    """Linie zawartosci hunka (z prefiksami ``+``, ``-``, `` ``)."""

    @property
    def total_lines(self) -> int:
        return 1 + len(self.body_lines)

    def render(self) -> str:
        return "\n".join([self.header] + self.body_lines)


@dataclass(slots=True)
class _ParsedFile:
    """Rozlozony diff jednego pliku: naglowek + hunki + meta."""

    file_path: str
    file_header: list[str]
    hunks: list[_Hunk]

    def render_header(self) -> str:
        return "\n".join(self.file_header)


@dataclass(slots=True)
class _FileHunk:
    """Jeden hunk z meta o pliku z ktorego pochodzi.

    Rzutowane do `DiffChunk.diff_text` w fazie renderingu. Trzyma
    caly parsed file zeby moc uzyc ``file_header`` tego pliku gdy
    chunk zaczyna sekcje tego pliku.
    """

    parsed: _ParsedFile
    hunk: _Hunk

    @property
    def file_path(self) -> str:
        return self.parsed.file_path

    @property
    def total_lines(self) -> int:
        return self.hunk.total_lines


def _parse_diff(diff_text: str, file_path: str) -> _ParsedFile:
    """Rozklada wyjscie ``git show --patch`` na naglowek + hunki."""

    if not diff_text:
        return _ParsedFile(file_path=file_path, file_header=[], hunks=[])

    lines = diff_text.splitlines()
    file_header: list[str] = []
    hunks: list[_Hunk] = []
    current_hunk: _Hunk | None = None

    for line in lines:
        if line.startswith("@@"):
            if current_hunk is not None:
                hunks.append(current_hunk)
            current_hunk = _Hunk(header=line, body_lines=[])
            continue

        if current_hunk is None:
            file_header.append(line)
        else:
            current_hunk.body_lines.append(line)

    if current_hunk is not None:
        hunks.append(current_hunk)

    return _ParsedFile(file_path=file_path, file_header=file_header, hunks=hunks)


@dataclass(slots=True)
class _HunkSplit:
    """Jeden kawalek podzielonego hunka wraz z meta splitu."""

    hunk: _Hunk
    part: int
    total: int
    split_group: str
    """Hash ORYGINALNEGO (niepodzielonego) hunka. Wspolny dla wszystkich
    kawalkow tego splitu \u2014 dzieki temu AI widzi ze nalezą do siebie."""


def _split_big_hunk_lines(
    hunk: _Hunk,
    max_lines: int,
    *,
    file_path: str,
) -> list[_HunkSplit]:
    """Dzieli hunk > max_lines na kawalki po ``max_lines - 1`` linii body.

    Kazdy kawalek ma ten sam header ``@@ ... @@`` (plus marker part X/Y).
    Splity zawsze dotycza jednego pliku (nie laczymy ich z innymi).
    Wszystkie kawalki dostaja identyczny ``split_group`` \u2014 hash calego
    (niepodzielonego) hunka \u2014 dzieki czemu mozna je potem odtworzyc
    w jedna logiczna grupe (AI i audyt).
    """

    body_budget = max(max_lines - 1, 1)

    original_text = hunk.render()
    split_group = _short_hash(f"{file_path}\n{original_text}")

    if len(hunk.body_lines) <= body_budget:
        return [_HunkSplit(hunk=hunk, part=1, total=1, split_group=split_group)]

    splits: list[_HunkSplit] = []
    total_parts = (len(hunk.body_lines) + body_budget - 1) // body_budget

    for part_idx in range(total_parts):
        start = part_idx * body_budget
        end = start + body_budget
        body = hunk.body_lines[start:end]
        marker_body = [
            f"# (czesc {part_idx + 1}/{total_parts} tego samego hunka, split_group={split_group})"
        ] + body
        piece = _Hunk(header=hunk.header, body_lines=marker_body)
        splits.append(
            _HunkSplit(
                hunk=piece,
                part=part_idx + 1,
                total=total_parts,
                split_group=split_group,
            )
        )

    return splits


@dataclass(slots=True)
class _ChunkBuilder:
    """Akumulator hunkow do jednego chunka.

    ``file_buckets`` to slownik ``file_path -> list[_Hunk]`` \u2014 zachowuje
    kolejnosc dodawania przez uzycie zwyklego dict (Python 3.7+).
    """

    file_parses: dict[str, _ParsedFile] = field(default_factory=dict)
    file_buckets: dict[str, list[_Hunk]] = field(default_factory=dict)
    line_budget: int = 0
    kind: ChunkKind = "full_hunks"

    def add(self, fh: _FileHunk) -> None:
        path = fh.file_path
        self.file_parses.setdefault(path, fh.parsed)
        self.file_buckets.setdefault(path, []).append(fh.hunk)
        self.line_budget += fh.total_lines

    def is_empty(self) -> bool:
        return not self.file_buckets

    def render(self) -> tuple[str, list[str], int]:
        """Renderuje zebrane hunki do tekstu diffa.

        Zwraca: ``(diff_text, file_paths, hunk_count)``.
        """

        sections: list[str] = []
        file_paths: list[str] = []
        hunk_count = 0

        for path, hunks in self.file_buckets.items():
            parsed = self.file_parses[path]
            header_lines = parsed.file_header
            body_parts: list[str] = list(header_lines)
            for h in hunks:
                body_parts.append(h.render())
            sections.append("\n".join(body_parts))
            file_paths.append(path)
            hunk_count += len(hunks)

        diff_text = "\n\n".join(sections)
        return diff_text, file_paths, hunk_count


def _build_chunk(
    builder: _ChunkBuilder,
    chunk_idx: int,
    kind: ChunkKind,
    *,
    split_group: str | None = None,
    split_part: int | None = None,
    split_total: int | None = None,
) -> DiffChunk | None:
    """Sklada ``DiffChunk`` z akumulatora. Zwraca None gdy pusty.

    Pola ``split_*`` sa wymagane gdy ``kind=split_hunk`` (walidacja na
    poziomie Pydantica w ``DiffChunk``) i forbidden dla ``full_hunks``.
    """

    if builder.is_empty():
        return None

    diff_text, file_paths, hunk_count = builder.render()
    if not diff_text.strip():
        return None

    return DiffChunk(
        chunk_idx=chunk_idx,
        total_chunks=chunk_idx,
        diff_text=diff_text,
        kind=kind,
        file_paths=file_paths,
        hunk_count=hunk_count,
        line_count=default_line_count(diff_text),
        split_group=split_group,
        split_part=split_part,
        split_total=split_total,
    )


def chunk_commit(
    changes: list[FileChange],
    *,
    max_diff_lines: int,
) -> list[DiffChunk]:
    """Chunkuje CALY commit \u2014 zbiera hunki wszystkich plikow, potem tnie.

    Flow:

    1. Iteruj po ``changes`` w kolejnosci (zachowuje kolejnosc zmian Gita).
    2. Dla kazdego pliku: ``_parse_diff`` \u2192 lista hunkow + file_header.
    3. Grupuj hunki (niezaleznie od pliku): dodajemy hunk do biezacego
       chunka jesli ``pending_lines + hunk.total_lines <= max_diff_lines``,
       inaczej flushujemy biezacy i zaczynamy nowy.
    4. Hunk > ``max_diff_lines`` przerywa grupowanie: flushujemy biezacy,
       rozbijamy hunk po liniach na splity, kazdy split \u2192 osobny chunk
       ``kind=split_hunk``, potem wznawiamy grupowanie.
    5. Na koncu poprawiamy ``total_chunks`` dla wszystkich chunkow.

    Specjalne przypadki:

    - Plik bez hunkow (rename bez zmian tresci) \u2192 jeden "marker" hunk
      z placeholderem ``# plik X: brak zmian tekstowych``, dodawany do
      biezacego bufora jak zwykly hunk.
    """

    if max_diff_lines < 1:
        raise ValueError(f"max_diff_lines musi byc >= 1, dostalem {max_diff_lines!r}")

    file_hunks: list[_FileHunk] = _collect_all_hunks(changes)
    if not file_hunks:
        return []

    chunks: list[DiffChunk] = []
    chunk_idx = 0
    builder = _ChunkBuilder()

    def flush() -> None:
        nonlocal chunk_idx, builder
        if builder.is_empty():
            return
        chunk_idx += 1
        chunk = _build_chunk(builder, chunk_idx, kind="full_hunks")
        if chunk is not None:
            chunks.append(chunk)
        builder = _ChunkBuilder()

    for fh in file_hunks:
        hunk_lines = fh.total_lines

        if hunk_lines > max_diff_lines:
            flush()
            splits = _split_big_hunk_lines(
                fh.hunk, max_lines=max_diff_lines, file_path=fh.file_path,
            )
            for split in splits:
                split_fh = _FileHunk(parsed=fh.parsed, hunk=split.hunk)
                split_builder = _ChunkBuilder(kind="split_hunk")
                split_builder.add(split_fh)
                chunk_idx += 1
                chunk = _build_chunk(
                    split_builder,
                    chunk_idx,
                    kind="split_hunk",
                    split_group=split.split_group,
                    split_part=split.part,
                    split_total=split.total,
                )
                if chunk is not None:
                    chunks.append(chunk)
            logger.info(
                "Chunker: hunk w pliku %s mial %d linii > max=%d, split na %d czesci "
                "(split_group=%s)",
                fh.file_path, hunk_lines, max_diff_lines, len(splits),
                splits[0].split_group if splits else "-",
            )
            continue

        if not builder.is_empty() and builder.line_budget + hunk_lines > max_diff_lines:
            flush()

        builder.add(fh)

    flush()

    total = len(chunks)
    normalized = [chunk.model_copy(update={"total_chunks": total}) for chunk in chunks]
    return normalized


def _collect_all_hunks(changes: list[FileChange]) -> list[_FileHunk]:
    """Parsuje kazdy ``FileChange`` i zwraca plaska liste hunkow z meta.

    Plik bez hunkow (pure rename, brak diffa) dostaje syntetyczny
    "marker" hunk ``# plik X: brak zmian tekstowych`` zeby AI widzial,
    ze plik jest obecny w commicie. Marker to jeden hunk o 1 linii body
    \u2014 zawsze mieszczacy sie w budzecie.
    """

    out: list[_FileHunk] = []
    for change in changes:
        parsed = _parse_diff(change.diff_text, change.path)

        if not parsed.hunks:
            marker = _Hunk(
                header=f"@@ -0,0 +0,0 @@ {change.path}: brak zmian tekstowych",
                body_lines=[f"# plik {change.path}: zmiana typu {change.change_type.value} bez tresci diffa"],
            )
            if not parsed.file_header:
                parsed = _ParsedFile(
                    file_path=parsed.file_path,
                    file_header=[
                        f"diff --git a/{change.path} b/{change.path}",
                        f"--- a/{change.path}" if change.old_path is None else f"--- a/{change.old_path}",
                        f"+++ b/{change.path}",
                    ],
                    hunks=[],
                )
            out.append(_FileHunk(parsed=parsed, hunk=marker))
            continue

        for hunk in parsed.hunks:
            out.append(_FileHunk(parsed=parsed, hunk=hunk))

    return out


__all__ = ["chunk_commit"]
