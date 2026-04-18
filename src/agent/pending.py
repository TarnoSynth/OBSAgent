"""Pending changes — zapis do vaulta z **diff-like** highlightem (red + green).

**Flow (Faza 6+ refaktor):**

1. Agent generuje ``ProposedPlan`` + plany MOC/indeksu (bez zmian).
2. ``ActionExecutor.apply_pending`` zapisuje zmiany do vaulta, ALE:
   - tresc kazdej ``ProposedWrite`` jest renderowana jako **diff-view**
     przez ``render_display_content`` — patrz tabelka nizej.
   - plany MOC/indeksu ida bez markerow (male, mechaniczne zmiany —
     highlight by je rozwalil, a user i tak ich bezposrednio nie czyta).
   - snapshot zachowuje poprzednie tresci **kazdego** ruszonego pliku
     (lub ``None`` gdy plik nie istnial przed apply) — to dane do
     rollbacku po odrzuceniu.
3. User przeglada vault w Obsidianie i odpowiada w terminalu:
   - ``T`` → ``finalize_pending`` nadpisuje pliki **czysta** trescia
     (znikaja i zielone, i czerwone markery), nastepnie ``commit_vault``.
   - ``n`` → ``rollback_pending`` przywraca vault ze snapshotu.

**Tabela diff-view per typ akcji:**

| Typ akcji | Zawartosc pliku w stanie pending                                        |
|-----------|-------------------------------------------------------------------------|
| create    | [frontmatter_new] + GREEN(body_new)                                     |
| update    | [frontmatter_new] + RED(body_previous) + GREEN(body_new)                |
| append    | previous_content (bez zmian) + GREEN(body_appended)                     |

- GREEN: callout ``[!tip]+`` — natywnie zielone tlo w Obsidianie
- RED:   callout ``[!failure]+`` — natywnie czerwone tlo w Obsidianie
- **Zadnych snippetow CSS po stronie usera nie trzeba** — callouts sa
  wbudowane.

**Dlaczego takie podejscie (``[!tip]`` / ``[!failure]`` zamiast HTML divow):**

- Callouts renderuja sie w **trybie czytania i live preview**; HTML div
  tylko w reading mode i psuje rendering markdownu w srodku.
- Callouts zachowuja semantyczne kolory zgodne z motywem usera
  (zielony dla ``tip``, czerwony dla ``failure``) — nie hardkodujemy
  hexów, nie walczymy z theme'ami.
- Frontmatter **MUSI** zostac poza podswietleniem — plugin Obsidian Git
  i Update Modified Date go czytaja; callout-wrapping by go zlamal.

**Zalozenia bezpieczenstwa:**

- Plugin Obsidian Git dziala w trybie **manualnym** (zgodnie z roadmapem).
  Jesli user ma autocommit, pending-version moze zostac zacommitowana
  przed akceptacja — terminal ostrzega przed tym wyraznie.
- Plugin "Update frontmatter modified date" dotyka **tylko** frontmattera
  (nasze markery sa w body) — nie koliduje.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from src.vault.manager import VaultManager


logger = logging.getLogger(__name__)


PENDING_START_MARKER = "<!--AGENT-PENDING-START-->"
PENDING_END_MARKER = "<!--AGENT-PENDING-END-->"

PREVIOUS_START_MARKER = "<!--AGENT-PREVIOUS-START-->"
PREVIOUS_END_MARKER = "<!--AGENT-PREVIOUS-END-->"

_PENDING_CALLOUT_HEADER_LINES: tuple[str, ...] = (
    "> [!tip]+ Agent: NOWA WERSJA — oczekuje akceptacji",
    "> Zielone tlo = tresc wygenerowana przez agenta dokumentacji.",
    "> Zatwierdzenie w terminalu (`T`) zostawia ta wersje, odrzucenie (`n`) cofa zmiany.",
    ">",
)

_PREVIOUS_CALLOUT_HEADER_LINES: tuple[str, ...] = (
    "> [!failure]+ Agent: POPRZEDNIA WERSJA — zostanie zastapiona",
    "> Czerwone tlo = tresc, ktora byla w pliku PRZED propozycja agenta.",
    "> Po zatwierdzeniu (`T`) ten blok znika; po odrzuceniu (`n`) wraca jako zywa tresc.",
    ">",
)

_FRONTMATTER_RE = re.compile(r"^(---\s*\n.*?\n---\s*(?:\n|$))", re.DOTALL)


def _split_frontmatter(content: str) -> tuple[str, str]:
    """Rozdziela (frontmatter_block, body). Jesli brak frontmattera — (``""``, content)."""

    if not content:
        return "", ""
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return "", content
    return match.group(1), content[match.end():]


def _quote_body_for_callout(body: str) -> str:
    """Prefixuje kazda linie body znakiem ``> `` (syntax callout Obsidiana).

    Puste linie dostaja sam ``>`` — bez tego callout sie urywa w Obsidianie.
    Koncowy newline jest zachowywany.
    """

    if not body:
        return ""

    trailing_newline = body.endswith("\n")
    lines = body.rstrip("\n").split("\n")
    quoted = "\n".join(f"> {line}" if line else ">" for line in lines)
    return quoted + ("\n" if trailing_newline else "")


def _wrap_body_in_callout(
    body: str,
    *,
    header_lines: tuple[str, ...],
    start_marker: str,
    end_marker: str,
) -> str:
    """Wspolny helper: wrap BODY (bez frontmattera) w callout + markery.

    Wolajacy jest odpowiedzialny za wyluskanie frontmattera — to pozwala
    uzyc tego samego helpera dla zielonego (``[!tip]+``) i czerwonego
    (``[!failure]+``) bloku w jednym pliku (render_display_content).
    """

    quoted_body = _quote_body_for_callout(body)
    header = "\n".join(header_lines) + "\n"
    inner = f"{header}{quoted_body}"
    if not inner.endswith("\n"):
        inner += "\n"
    return f"{start_marker}\n{inner}{end_marker}\n"


def wrap_pending_body(body: str) -> str:
    """Zawija samo BODY w zielony callout ``[!tip]+`` + markery pending.

    **Body**, a nie pelny content — frontmatter musi byc obslugiwany przez
    wolajacego (inaczej diff-view nie ma sensu — np. przy ``update``
    chcemy: frontmatter_nowy + RED(body_old) + GREEN(body_new)).
    """

    return _wrap_body_in_callout(
        body,
        header_lines=_PENDING_CALLOUT_HEADER_LINES,
        start_marker=PENDING_START_MARKER,
        end_marker=PENDING_END_MARKER,
    )


def wrap_previous_body(body: str) -> str:
    """Zawija samo BODY w czerwony callout ``[!failure]+`` + markery previous.

    Uzywane dla ``update`` — pokazuje userowi to, CO zostanie usuniete/zastapione.
    Dla ``append`` nic nie pokazujemy na czerwono (poprzednia tresc sie nie
    zmienia). Dla ``create`` takze nie (nic nie bylo).
    """

    return _wrap_body_in_callout(
        body,
        header_lines=_PREVIOUS_CALLOUT_HEADER_LINES,
        start_marker=PREVIOUS_START_MARKER,
        end_marker=PREVIOUS_END_MARKER,
    )


def wrap_pending(content: str) -> str:
    """Convenience dla ``create``: wyluska frontmatter + wraps body w zielony callout.

    Idempotentne: jesli ``content`` ma juz markery pending, zwraca bez zmian.
    Dla ``update`` / ``append`` uzyj ``render_display_content`` bezposrednio —
    on wie jak polozyc RED obok GREEN.
    """

    if has_pending_markers(content):
        return content

    frontmatter_block, body = _split_frontmatter(content)
    return frontmatter_block + wrap_pending_body(body)


def has_pending_markers(content: str) -> bool:
    """True jesli w tresci istnieje przynajmniej jedna para markerow pending."""

    return PENDING_START_MARKER in content and PENDING_END_MARKER in content


def has_previous_markers(content: str) -> bool:
    """True jesli w tresci istnieje przynajmniej jedna para markerow previous."""

    return PREVIOUS_START_MARKER in content and PREVIOUS_END_MARKER in content


def render_display_content(
    *,
    clean_content: str,
    previous_raw: str | None,
    had_wipe: bool,
) -> str:
    """Buduje finalna tresc pliku w stanie **pending** (diff view red+green).

    :param clean_content: calkowita **czysta** tresc pliku po wszystkich
        akcjach (to co trafi na dysk po akceptacji). Dla ``append`` zawiera
        cala poprzednia tresc + separator + dopisana; dla ``update`` jest
        to dokladnie ``action.content``.
    :param previous_raw: co bylo w pliku PRZED batchem (``None`` = nie istnial).
    :param had_wipe: True jesli na sciezce byl ``create`` lub ``update``
        (czyli poprzednia tresc zostala zastapiona). False dla append-only.

    Trzy tryby renderowania (zgodne z ``apply_pending``):

    1. **create** (``previous_raw is None``) → ``frontmatter_new + GREEN(body_new)``
    2. **update / wipe** (``previous_raw is not None and had_wipe``) →
       ``frontmatter_new + RED(body_prev) + GREEN(body_new)``
    3. **append-only** (``previous_raw is not None and not had_wipe``) →
       ``previous_raw + leading_sep + GREEN(delta)`` gdzie
       ``delta = clean_content[len(previous_raw):]``

    Scenariusz ``previous_raw is None and not had_wipe`` nie powinien sie
    zdarzyc (wolajacy powinien sprawdzic: append na nieistniejacy plik = blad).
    Fallback: traktujemy jak create.
    """

    if previous_raw is None:
        return wrap_pending(clean_content)

    if had_wipe:
        frontmatter_new, body_new = _split_frontmatter(clean_content)
        _, body_prev = _split_frontmatter(previous_raw)

        parts: list[str] = []
        if frontmatter_new:
            parts.append(frontmatter_new)
        if body_prev.strip():
            parts.append(wrap_previous_body(body_prev))
        if body_new.strip():
            parts.append(wrap_pending_body(body_new))
        return "".join(parts)

    if not clean_content.startswith(previous_raw):
        logger.warning(
            "render_display_content: append-only, ale clean_content nie zaczyna sie "
            "od previous_raw — fallback do pelnego wrap_pending(clean_content). "
            "(Prawdopodobnie multi-action zaburzyl stan; zachowujemy bezpieczenstwo.)"
        )
        return wrap_pending(clean_content)

    delta = clean_content[len(previous_raw):]
    delta_stripped = delta.lstrip("\n")
    leading_sep = delta[: len(delta) - len(delta_stripped)]

    if not leading_sep:
        leading_sep = "\n" if previous_raw.endswith("\n") else "\n\n"
    elif leading_sep == "\n" and not previous_raw.endswith("\n"):
        leading_sep = "\n\n"

    if not delta_stripped:
        return previous_raw

    return previous_raw + leading_sep + wrap_pending_body(delta_stripped)


class PendingBatch(BaseModel):
    """Stan po ``apply_pending`` — dane do ``finalize_pending`` albo ``rollback_pending``.

    Pola:

    - ``snapshot``: ``path -> previous_content`` dla **wszystkich** ruszonych
      plikow (akcje AI + plany MOC/index). ``None`` = plik nie istnial przed
      apply. Uzywane przez rollback.
    - ``clean_by_path``: ``path -> clean_content`` tylko dla sciezek zmienianych
      przez akcje AI — to jest tresc bez markerow pending, ktora zostanie
      zapisana przy ``finalize_pending`` (usuwa zielone tlo z vaulta).
    - ``plan_paths``: sciezki zmieniane przez plany MOC/index — zapisywane
      na dysk juz bez markerow (finalize ich nie dotyka, bo sa OK).
    - ``failed_action_paths``: sciezki, dla ktorych apply padlo — rollback
      dalej spojrzy na snapshot, ale finalize musi je pominac (nie ma
      clean content do zapisu — lub clean jest taki sam jak snapshot).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    snapshot: dict[str, str | None] = Field(default_factory=dict)
    clean_by_path: dict[str, str] = Field(default_factory=dict)
    plan_paths: list[str] = Field(default_factory=list)
    failed_action_paths: list[str] = Field(default_factory=list)
    wipe_paths: list[str] = Field(default_factory=list)
    """Sciezki dla ktorych byl ``create`` lub ``update`` (poprzednia tresc
    zastapiona). Uzywane przez UI do oznaczenia 'zawiera czerwony blok RED'."""
    create_paths: list[str] = Field(default_factory=list)
    """Sciezki dla ktorych byl ``create`` na wczesniej nieistniejacym pliku
    (czyli RED bloku nie ma — tylko GREEN)."""

    @property
    def all_touched_paths(self) -> list[str]:
        """Wszystkie sciezki ruszone w batchu — unikalne, w kolejnosci zapisu."""

        seen: set[str] = set()
        out: list[str] = []
        for path in list(self.clean_by_path.keys()) + list(self.plan_paths):
            if path not in seen:
                seen.add(path)
                out.append(path)
        return out

    @property
    def has_any_write(self) -> bool:
        """True jesli cokolwiek realnie zmodyfikowano na dysku (do commita / rollbacku)."""

        return bool(self.clean_by_path) or bool(self.plan_paths)


def capture_snapshot(
    vault_manager: VaultManager,
    rel_paths: list[str],
) -> dict[str, str | None]:
    """Odczytuje aktualna tresc kazdego pliku z listy (lub ``None`` gdy brak).

    **MUSI byc wolane PRZED apply_pending** — sniapshot opiera sie na stanie
    sprzed zapisu. Duplikaty sciezek sa deduplikowane.
    """

    snapshot: dict[str, str | None] = {}
    for rel_path in rel_paths:
        if rel_path in snapshot:
            continue
        try:
            if vault_manager.note_exists(rel_path):
                snapshot[rel_path] = vault_manager.read_text(rel_path)
            else:
                snapshot[rel_path] = None
        except Exception:
            logger.exception("capture_snapshot: nie udalo sie zczytac %s", rel_path)
            snapshot[rel_path] = None
    return snapshot


def restore_from_snapshot(
    vault_manager: VaultManager,
    snapshot: dict[str, str | None],
    *,
    only_paths: list[str] | None = None,
) -> list[str]:
    """Przywraca pliki ze snapshotu. Zwraca liste **faktycznie** przywrocconych.

    ``snapshot[path] is None`` → plik zostanie usuniety (jesli istnieje).
    ``snapshot[path] == "..."`` → plik zostanie nadpisany poprzednia trescia.

    ``only_paths`` zaweza zakres (np. do sciezek, ktore sie zapisaly).
    Gdy ``None`` — przywracamy wszystko ze snapshotu.

    Metoda jest **best-effort**: wyjatek przy jednym pliku loguje sie i
    lecimy dalej. User zobaczy w terminalu jesli cokolwiek sie nie udalo.
    """

    restored: list[str] = []
    paths_to_restore = only_paths if only_paths is not None else list(snapshot.keys())

    for rel_path in paths_to_restore:
        if rel_path not in snapshot:
            logger.warning("rollback: brak snapshotu dla %s — pomijam", rel_path)
            continue
        prev = snapshot[rel_path]
        try:
            if prev is None:
                if vault_manager.note_exists(rel_path):
                    vault_manager.delete(rel_path)
                    restored.append(rel_path)
            else:
                vault_manager.write_text(rel_path, prev)
                restored.append(rel_path)
        except Exception:
            logger.exception("rollback: nie udalo sie przywrocic %s", Path(rel_path).as_posix())

    return restored
