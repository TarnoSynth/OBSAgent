"""``build_frontmatter`` — jedno zrodlo prawdy dla YAML headera (Faza 5).

**Kontrakt:**

Wszystkie renderery domenowe (``render_hub``, ``render_concept``, ...)
wola **te** funkcje zamiast samodzielnie skladac ``---\\n...\\n---``.
Dzieki temu:

- **Kolejnosc kluczy jest deterministyczna** — ``type`` pierwszy, potem
  ``tags``, ``parent``, ``related``, ``status``, ``created``, ``updated``.
  Daje to stabilny diff w Gitie i pozwala zewnetrznym skryptom (Dataview
  queries) liczyc na sztywny layout.
- **Typy wartosci sa znormalizowane** — ``tags`` zawsze lista, nawet gdy
  model przekazal string; ``related`` zawsze lista wikilinkow ``[[X]]``;
  ``parent`` zawsze pojedynczy wikilink ``[[...]]``.
- **Wymagany tag = type** — jesli ``tags`` nie zawiera ``type`` (np.
  ``type: hub`` i brak ``hub`` w tagach), dodajemy go na poczatku
  listy. ``ConsistencyReport`` w Fazie 5 robi **twarda walidacje** —
  lepiej wyprodukowac poprawny frontmatter od razu niz lapac blad
  downstream.

**Co nie nalezy do tej warstwy:**

- Walidacja pol poza znormalizowaniem ich typu. Semantyka (np. ``parent``
  musi wskazywac na istniejacy MOC) lezy w `` ``consistency.py`` / w
  warstwie narzedzi write.
- Parsowanie istniejacego frontmattera. Do modyfikacji istniejacych
  notatek sluza helpery z ``tools/vault_write/_markdown_ops.py``
  (``parse_frontmatter`` / ``set_frontmatter_field``).

Zwroc uwage: format daty to ``YYYY-MM-DD``. Model dostaje w user prompcie
konkretna date commita — renderer **nie** skleja tego z ``datetime.now()``
zeby nie mieszac czasu biegu agenta z czasem zdarzenia projektowego.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

__all__ = ["build_frontmatter"]


_FIELD_ORDER: tuple[str, ...] = (
    "type",
    "tags",
    "parent",
    "related",
    "status",
    "created",
    "updated",
)

_DEFAULT_STATUS = "active"


def build_frontmatter(
    *,
    note_type: str,
    tags: Sequence[str] | None = None,
    parent: str | None = None,
    related: Sequence[str] | None = None,
    status: str | None = None,
    created: str,
    updated: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Buduje YAML frontmatter notatki — pelny blok ``---\\n...\\n---\\n``.

    :param note_type: wartosc pola ``type`` (np. ``"hub"``, ``"decision"``).
        Automatycznie dodawana do ``tags`` jako ``tag == type`` jesli jej
        tam nie ma (konwencja wymuszana przez ``ConsistencyReport``).
    :param tags: lista tagow (bez ``#``). ``None`` lub pusta = tylko tag
        odpowiadajacy ``note_type``.
    :param parent: wikilink do rodzica (MOC lub hub). Mozna podac ``"MOC___Core"``
        — renderer owinie w ``[[...]]``. Mozna tez przekazac gotowe ``"[[X]]"``.
        ``None`` = brak pola w frontmatterze.
    :param related: lista wikilinkow powiazanych. Elementy renderer
        normalizuje do formy ``[[X]]`` (podobnie jak ``parent``). Pusta
        lista → puste ``related: []`` (pole obecne, lista pusta — preferujemy
        to niz pomijanie, zeby user mogl od razu wiedziec, ze pole istnieje
        i moze dopisywac przez ``add_related_link``).
    :param status: ``active`` / ``draft`` / ``archived`` / ``deprecated``.
        ``None`` → default ``active``.
    :param created: data utworzenia ``YYYY-MM-DD`` — **wymagane**. Renderer
        nie wymysla dat, bo to data commita, nie biegu agenta.
    :param updated: data ostatniej aktualizacji ``YYYY-MM-DD``. ``None``
        → rowne ``created`` (typowy przypadek dla nowotworzonych notatek).
    :param extra: opcjonalny slownik dodatkowych pol YAML (np. ``role``
        dla technology). Dopisywane **na koniec** frontmattera, w
        kolejnosci dict. Celowo po standardowych polach — model latwiej
        czyta header gdy typ+meta sa u gory.

    :returns: blok ``---\\n...\\n---\\n`` gotowy do sklejenia z body.
        Zawsze konczy sie pojedynczym ``\\n`` (czyli cala notatka to
        ``frontmatter + body``, bez dodatkowego separatora).
    """

    if not isinstance(note_type, str) or not note_type.strip():
        raise ValueError("note_type musi byc niepustym stringiem")
    if not isinstance(created, str) or not created.strip():
        raise ValueError("created musi byc niepustym stringiem (YYYY-MM-DD)")

    note_type = note_type.strip()
    created = created.strip()
    updated = updated.strip() if isinstance(updated, str) and updated.strip() else created
    status = status.strip() if isinstance(status, str) and status.strip() else _DEFAULT_STATUS

    normalized_tags = _normalize_tags(tags, note_type=note_type)
    normalized_parent = _normalize_wikilink(parent) if parent else None
    normalized_related = _normalize_wikilink_list(related)

    fields: dict[str, Any] = {
        "type": note_type,
        "tags": normalized_tags,
    }
    if normalized_parent is not None:
        fields["parent"] = normalized_parent
    fields["related"] = normalized_related
    fields["status"] = status
    fields["created"] = created
    fields["updated"] = updated

    ordered: list[tuple[str, Any]] = [(k, fields[k]) for k in _FIELD_ORDER if k in fields]

    if extra:
        for k, v in extra.items():
            if not isinstance(k, str) or not k:
                continue
            if k in fields:
                continue
            ordered.append((k, v))

    lines = ["---"]
    for key, value in ordered:
        lines.append(_dump_field(key, value))
    lines.append("---")
    return "\n".join(lines) + "\n"


def _normalize_tags(tags: Sequence[str] | None, *, note_type: str) -> list[str]:
    """Zwraca liste tagow z gwarancja ``note_type.lower()`` jako pierwszy wpis.

    Duplikaty (case-insensitive) sa zwiniete — zachowujemy pierwsza
    napotkana postac (``"Module"`` wygrywa nad ``"module"`` jesli przyszlo
    pierwsze; rzadki przypadek, ale nie chcemy 2 formatow tego samego tagu).
    """

    required_tag = note_type.lower()
    result: list[str] = [required_tag]
    seen_lower: set[str] = {required_tag}

    if tags:
        for raw in tags:
            if not isinstance(raw, str):
                continue
            tag = raw.strip().lstrip("#")
            if not tag:
                continue
            key = tag.lower()
            if key in seen_lower:
                continue
            seen_lower.add(key)
            result.append(tag)

    return result


def _normalize_wikilink(raw: str) -> str:
    """Wraz ``"X"`` → ``"[[X]]"``; ``"[[X]]"`` zwraca bez zmian.

    Escape'y i aliasy (``[[X|alias]]``) zostaja zachowane. Biale znaki
    wokol sa trimowane.
    """

    value = raw.strip()
    if value.startswith("[[") and value.endswith("]]"):
        return value
    return f"[[{value}]]"


def _normalize_wikilink_list(raw: Iterable[str] | None) -> list[str]:
    """Kazdy element → wikilink ``[[X]]``; puste/None elementy pomijamy.

    Deduplikacja: po znormalizowanej wartosci (``[[X]]`` i ``X`` traktowane
    jako to samo).
    """

    result: list[str] = []
    seen: set[str] = set()
    if not raw:
        return result
    for item in raw:
        if not isinstance(item, str):
            continue
        stripped = item.strip()
        if not stripped:
            continue
        normalized = _normalize_wikilink(stripped)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _dump_field(key: str, value: Any) -> str:
    """Zwraca pojedyncza linie YAML-a dla pary key/value.

    Recznie renderujemy zeby kontrolowac format (cudzyslowy wokol wikilinkow,
    listy inline, brak sort_keys). ``yaml.safe_dump`` dalby poprawny wynik,
    ale inline'owana lista ``tags: [hub, core]`` jest czytelniejsza niz
    flow-multiline, a uproszczony renderer pokrywa wszystkie pola ktorych
    renderery uzywaja (string / int / lista stringow).
    """

    if isinstance(value, list):
        if not value:
            return f"{key}: []"
        rendered_items = [_dump_scalar(item) for item in value]
        return f"{key}: [{', '.join(rendered_items)}]"
    return f"{key}: {_dump_scalar(value)}"


def _dump_scalar(value: Any) -> str:
    """Render pojedynczej wartosci: wikilinki i stringi z ``:`` w cudzyslow."""

    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)

    text = str(value)
    needs_quoting = (
        text.startswith("[[")
        or ":" in text
        or "#" in text
        or text.strip() != text
        or text == ""
        or text.lower() in {"true", "false", "null", "yes", "no"}
    )
    if needs_quoting:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text
