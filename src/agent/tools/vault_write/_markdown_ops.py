"""Czyste funkcje parsowania markdowna dla granulowanych narzedzi (Faza 3).

**Rola:**

Narzedzia z Fazy 3 (``append_section``, ``replace_section``, ``add_table_row``,
``add_moc_link``, ``update_frontmatter``, ``add_related_link``) maja
chirurgicznie modyfikowac pliki .md — dopisac wiersz tabeli, podmienic
jedna sekcje, uzupelnic pole frontmattera. Zeby modul narzedziowy nie
duplikowal parsowania, caly parser zyje tutaj.

**Zasady modulu:**

- Czyste funkcje (bez stanu, bez I/O) — wejscie ``str``, wyjscie ``str``
  albo strukturowany wynik.
- Blad domenowy sygnalizowany przez wlasny wyjatek ``MarkdownOpsError`` —
  narzedzia lapia i mapuja na ``ToolResult(ok=False, error=...)``.
- Awareness fenced code-blocks (```` ``` ````) — nie matchujemy headingow
  ani tabel w srodku codefencow.
- YAML frontmatter obslugiwany przez ``pyyaml`` (ta sama zaleznosc co
  ``VaultManager`` — brak nowych depsow w Fazie 3).

**Czego NIE robi:**

- Nie siega do dysku. ``VaultManager`` jest w warstwie narzedzia, nie tu.
- Nie zna ``ToolResult`` / ``ProposedWrite``. To warstwa niezalezna — mozna
  testowac tylko na stringach.
- Nie waliduje sciezek (to robi ``vault_operations.validate_relative_md_path``).

**Dlaczego wlasny parser zamiast mistune/markdown-it-py:**

Zakres operacji jest waski (heading / tabela / frontmatter) i znany
z gory. Pelny parser AST znacznie skomplikowalby zwracanie "ten sam
plik + jeden zmieniony wiersz tabeli" (parser → modyfikacja AST →
re-rendering zmienia white-space, psuje diff). Line-based parsing z
fenced-code awareness daje idempotentne, diff-friendly wyjscie.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml


_FRONTMATTER_RE = re.compile(r"^(---\s*\n.*?\n---\s*(?:\n|$))", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^(\s*)(```+|~~~+)(.*)$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")


class MarkdownOpsError(ValueError):
    """Blad domenowy operacji markdown — wychwytywany przez narzedzie.

    Narzedzia (``append_section`` / ``replace_section`` / ...) lapia ten
    wyjatek i mapuja na ``ToolResult(ok=False, error=str(exc))`` — model
    dostaje czytelny komunikat "table 'Decyzje' not found" i moze sie
    poprawic w nastepnej iteracji.
    """


# ---------------------------------------------------------------------------
# Frontmatter YAML
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FrontmatterParts:
    """Rozbior pliku .md na frontmatter + body.

    :ivar data:            Wartosci YAML z frontmattera jako dict, albo
                           ``None`` gdy plik nie ma frontmattera.
    :ivar frontmatter_raw: Oryginalny, dokladny blok ``---\\n...---\\n``
                           (w tym końcowy newline jesli byl). Pusty string
                           gdy brak frontmattera.
    :ivar body:            Reszta pliku po bloku frontmattera (moze byc
                           pusta). Bez manipulacji newline'ami — zachowuje
                           dokladnie to, co bylo po ``---``.
    """

    data: dict[str, Any] | None
    frontmatter_raw: str
    body: str


def parse_frontmatter(content: str) -> FrontmatterParts:
    """Rozbija ``content`` na (data, frontmatter_raw, body).

    - Brak frontmattera → ``data=None``, ``frontmatter_raw=""``, ``body=content``.
    - Frontmatter ze zlym YAML → ``MarkdownOpsError`` (nie tlumimy —
      model ma widziec, ze plik jest uszkodzony zanim zaczniemy cos pisac).

    :param content: Surowa tresc pliku .md.
    :raises MarkdownOpsError: gdy frontmatter istnieje ale YAML jest
        niepoprawny (np. niezamkniete cudzyslowy, bledny typ).
    """

    if not content:
        return FrontmatterParts(data=None, frontmatter_raw="", body="")
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return FrontmatterParts(data=None, frontmatter_raw="", body=content)

    raw = match.group(1)
    body = content[match.end():]
    inner = raw.strip()
    assert inner.startswith("---") and inner.endswith("---"), (
        f"parse_frontmatter: spodziewany format '---...---', dostalismy {raw!r}"
    )
    inner_yaml = inner[3:-3].strip("\n")
    try:
        loaded = yaml.safe_load(inner_yaml) if inner_yaml else {}
    except yaml.YAMLError as exc:
        raise MarkdownOpsError(f"Frontmatter YAML sie nie parsuje: {exc}") from exc

    if loaded is None:
        data: dict[str, Any] = {}
    elif isinstance(loaded, dict):
        data = loaded
    else:
        raise MarkdownOpsError(
            f"Frontmatter musi byc mapa YAML, dostalismy {type(loaded).__name__}"
        )

    return FrontmatterParts(data=data, frontmatter_raw=raw, body=body)


def dump_frontmatter(data: dict[str, Any]) -> str:
    """Serializuje dict do bloku ``---\\n...\\n---\\n``.

    Uzywa ``yaml.safe_dump`` z ``sort_keys=False`` (zachowuje kolejnosc
    wejsciowego dict) i ``allow_unicode=True``. Nie dodajemy pustego dict
    jako ``{}`` — dla pustego wejscia zwracamy ``---\\n---\\n``.
    """

    if not data:
        return "---\n---\n"
    body = yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    if not body.endswith("\n"):
        body += "\n"
    return f"---\n{body}---\n"


def set_frontmatter_field(content: str, field: str, value: Any) -> str:
    """Ustawia pojedyncze pole we frontmatterze. Body pozostaje nietkniete.

    - Brak frontmattera w pliku → tworzymy nowy z jednym polem.
    - ``value`` zastepuje istniejaca wartosc (list zastapi list, nie dopisuje).
      Do dopisywania pojedynczych wpisow do listy uzyj ``add_to_frontmatter_list``.
    - Pozostale pola frontmattera zachowuja kolejnosc i wartosci.

    :raises MarkdownOpsError: gdy frontmatter jest nieparsowalny (delegujemy
        z ``parse_frontmatter``).
    """

    if not field or not isinstance(field, str):
        raise MarkdownOpsError("field musi byc niepustym stringiem")

    parts = parse_frontmatter(content)
    data = dict(parts.data) if parts.data is not None else {}
    data[field] = value
    return dump_frontmatter(data) + parts.body


def add_to_frontmatter_list(
    content: str, field: str, value: Any, *, deduplicate: bool = True,
) -> tuple[str, bool]:
    """Dopisuje pojedynczy wpis do listy we frontmatterze. Idempotentne.

    Uzywane przez ``add_related_link`` i przyszle ``add_tag`` — tam, gdzie
    pole frontmattera to lista (``related``, ``tags``), ``set_frontmatter_field``
    by zastapilo cala liste.

    :param field: nazwa pola (np. ``"related"``).
    :param value: pojedyncza wartosc do dopisania (string, wikilink, itp.).
    :param deduplicate: True = nie dodawaj jesli juz jest (porownanie po wartosci).
    :returns: tuple ``(new_content, added)``. ``added=False`` gdy
        ``deduplicate=True`` i wpis juz istnieje — zwracamy content
        nietkniety.
    :raises MarkdownOpsError: gdy pole istnieje ale nie jest lista.
    """

    parts = parse_frontmatter(content)
    data = dict(parts.data) if parts.data is not None else {}

    current = data.get(field)
    if current is None:
        new_list = [value]
    elif isinstance(current, list):
        if deduplicate and value in current:
            return content, False
        new_list = list(current) + [value]
    else:
        raise MarkdownOpsError(
            f"Pole frontmattera '{field}' istnieje ale nie jest lista "
            f"(jest {type(current).__name__}). Uzyj 'update_frontmatter' zeby zastapic calosc."
        )

    data[field] = new_list
    return dump_frontmatter(data) + parts.body, True


# ---------------------------------------------------------------------------
# Headings (body-awareness: code fences skipped)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HeadingSpan:
    """Zakres liniowy pojedynczej sekcji pod headingiem.

    :ivar level:         Poziom headingu (2 dla ``##``, 3 dla ``###``, ...).
    :ivar title:         Tekst headingu (bez ``#`` i bez wiodacego/trailing ws).
    :ivar heading_line:  Indeks linii headingu (0-based, wzgledem ``lines``).
    :ivar body_start:    Pierwsza linia tresci sekcji (heading_line + 1).
    :ivar body_end:      Indeks linii PIERWSZEJ linii NASTEPNEJ sekcji
                         (equal-or-higher level). ``len(lines)`` gdy sekcja
                         konczy plik. Range ``[body_start, body_end)`` ≡
                         tresc sekcji bez headingu.
    """

    level: int
    title: str
    heading_line: int
    body_start: int
    body_end: int


def _split_lines_preserving(content: str) -> tuple[list[str], bool]:
    """Rozbija ``content`` na linie, zwracajac ``(lines, had_trailing_newline)``.

    ``str.splitlines()`` bez argumentow gubi info "czy plik konczyl sie \\n".
    Tu to zachowujemy, zeby re-join odtworzyl dokladnie oryginal (idempotencja).
    """

    if not content:
        return [], False
    had_trailing = content.endswith("\n")
    raw = content.split("\n")
    if had_trailing:
        raw = raw[:-1]
    return raw, had_trailing


def _join_lines(lines: list[str], had_trailing_newline: bool) -> str:
    """Odwrotnosc ``_split_lines_preserving``."""

    if not lines:
        return "\n" if had_trailing_newline else ""
    return "\n".join(lines) + ("\n" if had_trailing_newline else "")


def _iter_code_fence_mask(lines: list[str]) -> list[bool]:
    """Dla kazdej linii zwraca True jesli jest w srodku code-fence.

    Linia otwierajaca i zamykajaca fence rowniez dostaje ``True`` — zeby
    zadna operacja (heading / tabela) nie modyfikowala fragmentu kodu
    ani jego granicy.

    Rozpoznajemy ``` i ~~~ z dowolna liczba ``+`` / ``~`` (ale przynajmniej 3).
    Otwierajacy fence ma opcjonalny info-string (np. ```python). Zamykajacy
    MUSI miec taka sama liczbe znaczkow. Uproszczone: rozpoznajemy po minimum
    3 znakach tego samego typu, i "zamyka" fence pierwsza linia z 3+ znakami
    tego samego typu po otwarciu.
    """

    in_fence = False
    fence_char: str | None = None
    fence_len = 0
    mask: list[bool] = []
    for line in lines:
        match = _FENCE_RE.match(line)
        if match:
            ticks = match.group(2)
            if not in_fence:
                in_fence = True
                fence_char = ticks[0]
                fence_len = len(ticks)
                mask.append(True)
                continue
            if ticks[0] == fence_char and len(ticks) >= fence_len:
                mask.append(True)
                in_fence = False
                fence_char = None
                fence_len = 0
                continue
            mask.append(True)
            continue
        mask.append(in_fence)
    return mask


def _find_heading_spans(lines: list[str], in_fence: list[bool]) -> list[HeadingSpan]:
    """Zwraca wszystkie headingi pliku z ich zakresami tresci."""

    spans: list[HeadingSpan] = []
    for idx, line in enumerate(lines):
        if in_fence[idx]:
            continue
        match = _HEADING_RE.match(line)
        if not match:
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        spans.append(
            HeadingSpan(
                level=level,
                title=title,
                heading_line=idx,
                body_start=idx + 1,
                body_end=len(lines),
            )
        )

    for i, span in enumerate(spans):
        end = len(lines)
        for later in spans[i + 1:]:
            if later.level <= span.level:
                end = later.heading_line
                break
        span.body_end = end
    return spans


def find_heading_span(content: str, heading: str) -> HeadingSpan | None:
    """Znajduje pierwszy heading o podanym tytule (case-sensitive, trim).

    Match ignoruje poziom (``##`` vs ``###``) — rozpoznajemy tytul.
    Gdyby to bylo za liberalne, caller moze zawsze zawezic przez sprawdzenie
    ``span.level`` po zwrocie.

    :returns: ``HeadingSpan`` albo ``None``.
    """

    if not heading or not isinstance(heading, str):
        raise MarkdownOpsError("heading musi byc niepustym stringiem")
    target = heading.strip()
    if not target:
        raise MarkdownOpsError("heading nie moze byc samymi bialymi znakami")

    lines, _ = _split_lines_preserving(content)
    in_fence = _iter_code_fence_mask(lines)
    for span in _find_heading_spans(lines, in_fence):
        if span.title == target:
            return span
    return None


def append_section(
    content: str,
    heading: str,
    section_body: str,
    *,
    level: int = 2,
) -> str:
    """Dopisuje nowa sekcje ``## heading\\n\\n{body}`` na koncu pliku.

    :raises MarkdownOpsError: gdy sekcja o takim tytule juz istnieje
        (uzyj ``replace_section``).

    :param level: poziom headingu (2 = ``##``, 3 = ``###``). Zakres 1-6.
    """

    if level < 1 or level > 6:
        raise MarkdownOpsError(f"level musi byc w zakresie 1-6, dostalismy {level!r}")

    existing = find_heading_span(content, heading)
    if existing is not None:
        raise MarkdownOpsError(
            f"Sekcja o headingu {heading!r} juz istnieje (poziom {existing.level}). "
            f"Uzyj 'replace_section' zeby podmienic jej tresc."
        )

    prefix = "#" * level
    heading_line = f"{prefix} {heading.strip()}"
    body = section_body or ""
    if body and not body.startswith("\n"):
        body = "\n" + body
    if body and not body.endswith("\n"):
        body = body + "\n"
    if not body:
        body = "\n"

    base = content or ""
    if not base:
        return f"{heading_line}\n{body}"
    if base.endswith("\n\n"):
        separator = ""
    elif base.endswith("\n"):
        separator = "\n"
    else:
        separator = "\n\n"
    return f"{base}{separator}{heading_line}\n{body}"


def replace_section(content: str, heading: str, new_body: str) -> str:
    """Podmienia tresc sekcji pod podanym headingiem (body bez headingu).

    Granica sekcji: od linii PO headingu do pierwszej linii nastepnego
    headingu o tym samym lub wyzszym poziomie (albo konca pliku).

    ``new_body`` to sam body — NIE zawiera linii headingu. Moze byc pusty
    (wtedy sekcja zostanie scalona do samego headingu + pustej linii).

    :raises MarkdownOpsError: gdy heading nie istnieje.
    """

    span = find_heading_span(content, heading)
    if span is None:
        raise MarkdownOpsError(
            f"Heading {heading!r} nie znaleziony. Uzyj 'append_section' zeby utworzyc."
        )

    lines, had_trailing = _split_lines_preserving(content)
    before = lines[: span.body_start]
    after = lines[span.body_end:]

    body_text = new_body or ""
    if body_text and not body_text.endswith("\n"):
        body_text = body_text + "\n"

    body_lines: list[str]
    if not body_text:
        body_lines = [""]
    else:
        body_lines, _ = _split_lines_preserving(body_text)
        if body_text.endswith("\n"):
            body_lines.append("")
    if not body_lines or body_lines[-1] != "":
        body_lines.append("")

    new_lines = before + body_lines + after
    return _join_lines(new_lines, had_trailing)


# ---------------------------------------------------------------------------
# Tables (GFM-style pipe tables)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TableSpan:
    """Zakres liniowy tabeli markdown.

    :ivar header_line:    Indeks linii naglowka tabeli (``| col1 | col2 |``).
    :ivar separator_line: Indeks linii separatora (``|---|---|``). Zwykle
                          ``header_line + 1``.
    :ivar last_row_line:  Indeks OSTATNIEJ linii danych (moze rownac sie
                          ``separator_line`` gdy tabela jest pusta).
    :ivar headers:        Rozparsowane komorki naglowka (bez ``|``, strip).
    """

    header_line: int
    separator_line: int
    last_row_line: int
    headers: list[str]


def _parse_pipe_row(line: str) -> list[str] | None:
    """Rozbija linie tabeli ``| a | b | c |`` na ``["a","b","c"]``.

    Brzegowe ``|`` sa opcjonalne (``a | b | c`` tez OK). Escapowany ``\\|``
    zostaje zachowany w komorce. Zwraca ``None`` gdy linia nie wyglada na
    wiersz tabeli (brak jakiegokolwiek ``|`` poza escape'ami).
    """

    if not line or line.strip() == "":
        return None
    stripped = line.strip()

    cells: list[str] = []
    buf: list[str] = []
    escaped = False
    for ch in stripped:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            buf.append(ch)
            continue
        if ch == "|":
            cells.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    cells.append("".join(buf))

    if stripped.startswith("|"):
        if cells and cells[0] == "":
            cells = cells[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        if cells and cells[-1] == "":
            cells = cells[:-1]

    if len(cells) <= 1:
        return None
    return [c.strip() for c in cells]


def _find_tables_under(
    lines: list[str],
    in_fence: list[bool],
    span: HeadingSpan,
) -> list[TableSpan]:
    """Znajduje wszystkie tabele w body sekcji ``span``."""

    tables: list[TableSpan] = []
    i = span.body_start
    end = span.body_end
    while i < end:
        if in_fence[i]:
            i += 1
            continue
        header_line = lines[i]
        header_cells = _parse_pipe_row(header_line)
        if header_cells is None:
            i += 1
            continue
        if i + 1 >= end or in_fence[i + 1]:
            i += 1
            continue
        sep_line = lines[i + 1]
        if not _TABLE_SEPARATOR_RE.match(sep_line):
            i += 1
            continue
        last_row = i + 1
        j = i + 2
        while j < end and not in_fence[j]:
            row = _parse_pipe_row(lines[j])
            if row is None:
                break
            last_row = j
            j += 1
        tables.append(
            TableSpan(
                header_line=i,
                separator_line=i + 1,
                last_row_line=last_row,
                headers=header_cells,
            )
        )
        i = j
    return tables


def find_first_table_under_heading(
    content: str,
    heading: str,
) -> tuple[HeadingSpan, TableSpan] | None:
    """Zwraca pierwsza tabele w sekcji o podanym tytule.

    :returns: ``(heading_span, table_span)`` albo ``None`` (brak headingu
        albo brak tabeli w sekcji).
    """

    span = find_heading_span(content, heading)
    if span is None:
        return None
    lines, _ = _split_lines_preserving(content)
    in_fence = _iter_code_fence_mask(lines)
    tables = _find_tables_under(lines, in_fence, span)
    if not tables:
        return None
    return span, tables[0]


def add_table_row(content: str, heading: str, cells: list[str]) -> str:
    """Dopisuje wiersz do pierwszej tabeli pod sekcja ``heading``.

    - Liczba komorek MUSI pasowac do naglowka tabeli (inaczej
      ``MarkdownOpsError`` z aktualnym naglowkiem).
    - Wiersz jest dopisywany po OSTATNIM wierszu danych (albo tuz po
      separatorze gdy tabela jest pusta).

    :raises MarkdownOpsError: brak sekcji, brak tabeli w sekcji,
        arity mismatch.
    """

    found = find_first_table_under_heading(content, heading)
    if found is None:
        span = find_heading_span(content, heading)
        if span is None:
            raise MarkdownOpsError(
                f"Heading {heading!r} nie znaleziony. Utworz sekcje (append_section) "
                f"lub sprawdz pisownie."
            )
        raise MarkdownOpsError(
            f"Pod headingiem {heading!r} nie znaleziono tabeli markdown "
            f"(|col|col|\\n|---|---|). Dodaj tabele przez 'replace_section' "
            f"zanim wywolasz 'add_table_row'."
        )

    _, table = found
    if not isinstance(cells, list) or not cells:
        raise MarkdownOpsError("cells musi byc niepusta lista stringow")
    if any(not isinstance(c, str) for c in cells):
        raise MarkdownOpsError("kazda komorka w 'cells' musi byc stringiem")
    if len(cells) != len(table.headers):
        raise MarkdownOpsError(
            f"Arity mismatch: tabela ma {len(table.headers)} kolumn "
            f"({table.headers}), dostalismy {len(cells)} komorek."
        )

    escaped_cells = [_escape_pipe(c.strip()) for c in cells]
    new_row = "| " + " | ".join(escaped_cells) + " |"

    lines, had_trailing = _split_lines_preserving(content)
    insert_at = table.last_row_line + 1
    new_lines = lines[:insert_at] + [new_row] + lines[insert_at:]
    return _join_lines(new_lines, had_trailing)


def _escape_pipe(cell: str) -> str:
    """Escape ``|`` wewnatrz komorki tabeli (GFM syntax)."""

    return cell.replace("|", r"\|")


# ---------------------------------------------------------------------------
# MOC bullet lists
# ---------------------------------------------------------------------------


def add_bullet_link_under_heading(
    content: str,
    heading: str,
    wikilink: str,
    *,
    description: str | None = None,
) -> tuple[str, bool]:
    """Dopisuje ``- [[wikilink]] — description`` pod sekcja headingu.

    Idempotentne: jesli bullet z ta sama ``[[wikilink]]`` juz jest w sekcji,
    zwraca ``(content, False)`` bez modyfikacji (description nie jest
    przedmiotem porownania — przyjmujemy, ze wikilink identyfikuje wpis).

    Wiersz jest dopisywany PO ostatnim istniejacym bullecie w sekcji
    (albo tuz po headingu, gdy sekcja jest pusta).

    :param heading: tytul sekcji (bez ``##``).
    :param wikilink: docelowa notatka, np. ``"[[Auth]]"`` lub
        ``"[[Auth|alias]]"``. Dopuszczamy ``"Auth"`` — wtedy owiniemy
        w ``[[...]]`` sami.
    :param description: opcjonalny dopisek po ``—``.
    :returns: ``(new_content, added)``. ``added=False`` gdy juz byl.
    :raises MarkdownOpsError: gdy heading nie istnieje.
    """

    span = find_heading_span(content, heading)
    if span is None:
        raise MarkdownOpsError(
            f"Heading {heading!r} nie znaleziony w pliku — nie ma gdzie dopisac linku."
        )

    target = wikilink.strip()
    if not target:
        raise MarkdownOpsError("wikilink nie moze byc pusty")
    if not (target.startswith("[[") and target.endswith("]]")):
        target = f"[[{target}]]"

    lines, had_trailing = _split_lines_preserving(content)
    section_slice = lines[span.body_start: span.body_end]
    in_fence = _iter_code_fence_mask(lines)

    for idx_in_section, raw in enumerate(section_slice):
        absolute = span.body_start + idx_in_section
        if in_fence[absolute]:
            continue
        if _bullet_contains_wikilink(raw, target):
            return content, False

    desc_part = f" — {description.strip()}" if description and description.strip() else ""
    new_line = f"- {target}{desc_part}"

    last_bullet_absolute = -1
    for idx_in_section, raw in enumerate(section_slice):
        absolute = span.body_start + idx_in_section
        if in_fence[absolute]:
            continue
        if _looks_like_bullet(raw):
            last_bullet_absolute = absolute

    if last_bullet_absolute >= 0:
        insert_at = last_bullet_absolute + 1
    else:
        insert_at = span.body_start
        while insert_at < span.body_end and lines[insert_at].strip() == "":
            insert_at += 1

    new_lines = lines[:insert_at] + [new_line] + lines[insert_at:]
    return _join_lines(new_lines, had_trailing), True


_BULLET_RE = re.compile(r"^\s*[-*+]\s+")


def _looks_like_bullet(line: str) -> bool:
    """Wiersz zaczyna sie ``-`` / ``*`` / ``+`` + spacja (Markdown bullet)."""

    return bool(_BULLET_RE.match(line))


def _bullet_contains_wikilink(line: str, wikilink: str) -> bool:
    """True jesli linia bulletu zawiera dokladnie ten wikilink (z ``[[...]]``)."""

    if not _looks_like_bullet(line):
        return False
    return wikilink in line


__all__ = [
    "FrontmatterParts",
    "HeadingSpan",
    "MarkdownOpsError",
    "TableSpan",
    "add_bullet_link_under_heading",
    "add_table_row",
    "add_to_frontmatter_list",
    "append_section",
    "dump_frontmatter",
    "find_first_table_under_heading",
    "find_heading_span",
    "parse_frontmatter",
    "replace_section",
    "set_frontmatter_field",
]
