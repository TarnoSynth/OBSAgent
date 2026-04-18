"""Safety-net pre-compute dla MOC i ``_index.md`` (Faza 7 cleanup).

**Rola po Fazie 7:**

Glowna logika dopisywania linkow do MOC oraz utrzymania ``_index.md``
siedzi teraz w **narzedziach** (``add_moc_link``, ``create_hub``,
``create_*`` domain creators) — agent sam deklaruje te zmiany przez
tool cally. Ten modul **nie duplikuje** juz tej roli. Zostaje jako
**safety net** dla edge-case'ow:

- notatka ``create`` bez ``parent`` we frontmatterze i bez rownoczesnego
  tool calla ``add_moc_link`` (np. model zapomnial) — wtedy wyprowadzamy
  brakujacy wpis z heurystyki (``MOCManager.find_moc_for_note``);
- ``_index.md`` auto-indeks typow — zostaje zaktualizowany wylacznie dla
  ``create`` nowych notatek, bez ktorych model i tak nie dostaje go
  w ``list_notes`` (usability compromise).

**Co NIE jest robione po Fazie 7:**

- Dopisywanie MOC linkow dla akcji ``update`` / ``append`` — model
  decyduje samodzielnie przez tool cally (ikoniczna pozycja: user moze
  update'owac hub bez linku w MOC-u, co jest legalna operacja).
- Linkowanie juz zlinkowanych notatek (idempotencja sprawdzana przez
  ``_moc_contains_link``).

Preview dalej musi zobaczyc **pelny** zestaw zmian — wlacznie z tymi,
ktore doklada ta warstwa — zeby user widzial calosc commita przed
``[T/n]``. Dlatego emitujemy ``PlannedVaultWrite`` a nie zapisujemy
od razu.

Algorytm dla wielu akcji: iterujemy po akcjach kolejno i aktualizujemy
**zywa kopie** tresci MOC / index w pamieci. Na koncu emitujemy jeden
``PlannedVaultWrite`` per plik (jesli byly zmiany). Dzieki temu nie
trzeba doklejac ``append-over-append`` w executorze.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from src.agent.models_actions import ProposedWrite
from src.agent.tools.vault_write.register_pending_concept import PENDING_CONCEPTS_PATH
from src.vault.manager import VaultManager
from src.vault.models import VaultKnowledge, VaultNote
from src.vault.moc import DEFAULT_INDEX_PATH, MOCManager


PlanKind = Literal["moc_append", "index_update", "index_create"]


class PlannedVaultWrite(BaseModel):
    """Jedna planowana zmiana w pliku MOC / indeksu (bez zapisu).

    ``kind``:

    - ``moc_append``    \u2014 dopisanie ``- [[stem]]`` do pliku MOC
    - ``index_update``  \u2014 wstawienie wpisu w istniejacym ``_index.md``
    - ``index_create``  \u2014 utworzenie calego ``_index.md`` od zera

    ``new_content`` to **docelowa tresc calego pliku** po operacji. Executor
    zrobi ``VaultManager.write_text(path, new_content)`` \u2014 jedno zapis
    na kazdy plan.

    ``preview_lines`` to lista kazdej nowej linii (np. 3 nowe wpisy w MOC
    \u2192 3 elementy). Preview UI pokazuje je jako bulletsy pod naglowkiem
    pliku.
    """

    kind: PlanKind
    path: str
    reason: str = Field(..., description="Human-readable opis dla preview")
    preview_lines: list[str] = Field(default_factory=list)
    new_content: str


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)
_WIKILINK_TARGET_RE = re.compile(r"^\[\[([^\]]+)\]\]$")


def plan_post_action_updates(
    actions: list[ProposedWrite],
    vault_manager: VaultManager,
    knowledge: VaultKnowledge,
    *,
    index_path: str = DEFAULT_INDEX_PATH,
) -> list[PlannedVaultWrite]:
    """Safety-net: dokleja brakujace MOC / index wpisy dla ``ProposedWrite``.

    Po Fazie 7 glowna logika dopinania MOC linkow i indeksu siedzi w
    tool'ach (``add_moc_link``, ``create_hub``, domain creators). Ta
    funkcja dziala **tylko jako siec bezpieczenstwa**: jesli model
    stworzyl notatke ``type: hub/concept/...`` bez ``parent`` we
    frontmatterze **i** bez osobnego ``add_moc_link`` w tej samej sesji
    — wtedy wyprowadzamy brakujacy wpis z heurystyki (``MOCManager.
    find_moc_for_note``).

    Algorytm:

    1. Pobierz obecne tresci plikow MOC kandydatow + indeksu (w pamieci).
    2. Wyznacz zestaw MOC'ow juz dopisanych przez model (path'y MOC-owych
       ``ProposedWrite``) — tych nie ruszamy (model sam zadbal).
    3. Dla notatek ``create``/``update`` bez ``parent`` w FM dopisz
       ``- [[stem]]`` do sugerowanego MOC jesli tamten jeszcze nie linkuje.
    4. Dla ``create`` auto-aktualizuj ``_index.md`` (usability: bez tego
       model nie widzi nowosci w ``list_notes`` z filtra typu).
    5. Emit po jednym ``PlannedVaultWrite`` per plik, ktorego tresc sie
       zmienila.

    Zwraca: plany MOC (posortowane po sciezce) + plany index (max 1).
    """

    moc_manager = MOCManager(vault_manager)

    moc_state: dict[str, _FileDraft] = {}
    moc_preview: dict[str, list[str]] = {}

    index_draft: _FileDraft | None = None
    index_created_now = False
    index_preview_lines: list[str] = []

    # Faza 7 safety-net: MOC pliki, ktore model sam zaktualizowal w tej
    # sesji (np. przez ``add_moc_link`` lub ``create_hub`` z parent_moc).
    # Dla nich NIE dokladamy nic post-hoc — zakladamy, ze model zadbal
    # o zlinkowanie wszystkich swoich notatek swiadomie.
    tool_touched_mocs = {
        action.path for action in actions
        if _looks_like_moc_path(action.path)
    }

    for action in actions:
        if action.type not in ("create", "update", "append"):
            continue

        # Faza 6: ``_Pending_Concepts.md`` to notatka-sluga (indeks placeholderow),
        # nie wezel merytoryczny — nie dopisujemy jej do zadnego MOC ani do
        # ``_index.md``. Register_pending_concept tworzy/aktualizuje ten plik
        # przez granularny write, ale auto-utrzymanie grafu go ignoruje.
        if action.path == PENDING_CONCEPTS_PATH:
            continue

        note = _infer_note_from_action(action, vault_manager)
        if note is None:
            continue

        # Notatki z ``type: index`` (np. sam ``_index.md``, auto-indeksy)
        # tez nie powinny trafiac do MOC/auto-index, identyczna logika jak dla
        # type: moc. Zostawiamy je w spokoju.
        if (note.type or "").lower() == "index":
            continue

        # Faza 7: safety-net jest aktywny TYLKO dla notatek ``create``, ktore
        # nie maja ``parent`` we frontmatterze. Jesli model ustawil parent
        # albo notatka jest tylko aktualizowana — respektujemy jego decyzje
        # (model wie lepiej, narzedzia mial i z nich skorzystal).
        if action.type == "create" and not note.parent and not _is_moc_note(note):
            moc = moc_manager.find_moc_for_note(note, knowledge=knowledge)
            if moc is not None and moc.path not in tool_touched_mocs:
                moc_path = moc.path
                draft = moc_state.get(moc_path)
                if draft is None:
                    draft = _FileDraft(initial=vault_manager.read_text(moc_path))
                    moc_state[moc_path] = draft

                stem = Path(note.path).stem
                if not _moc_contains_link(draft.current, stem):
                    draft.current = _append_line(draft.current, f"- [[{stem}]]")
                    moc_preview.setdefault(moc_path, []).append(f"- [[{stem}]]")

        if action.type == "create":
            # _section_title_for stalo sie instance method po parametryzacji
            # moc_pattern (Faza 0 refaktoru tool loop) \u2014 wolamy przez instancje
            # zeby korzystac z pattern-aware rozpoznawania MOC-ow.
            section = moc_manager._section_title_for(note)
            entry = f"- [[{Path(note.path).stem}]]"

            if index_draft is None:
                if vault_manager.note_exists(index_path):
                    index_draft = _FileDraft(initial=vault_manager.read_text(index_path))
                else:
                    seed = MOCManager._render_initial_index(section, entry)  # type: ignore[attr-defined]
                    index_draft = _FileDraft(initial="", current=seed)
                    index_created_now = True
                    index_preview_lines.append(f"sekcja '{section}': {entry}")
                    continue

            if MOCManager._entry_already_in_section(  # type: ignore[attr-defined]
                index_draft.current, section, entry
            ):
                continue
            new_text = MOCManager._insert_entry_into_index(  # type: ignore[attr-defined]
                index_draft.current, section, entry
            )
            if new_text != index_draft.current:
                index_draft.current = new_text
                index_preview_lines.append(f"sekcja '{section}': {entry}")

    moc_plans: list[PlannedVaultWrite] = []
    for moc_path, draft in moc_state.items():
        if draft.current == draft.initial:
            continue
        lines = moc_preview.get(moc_path, [])
        moc_plans.append(
            PlannedVaultWrite(
                kind="moc_append",
                path=moc_path,
                reason=f"dopisanie {len(lines)} linka(ow) do MOC",
                preview_lines=lines,
                new_content=draft.current,
            )
        )

    index_plans: list[PlannedVaultWrite] = []
    if index_draft is not None and index_draft.current != index_draft.initial:
        index_plans.append(
            PlannedVaultWrite(
                kind="index_create" if index_created_now else "index_update",
                path=index_path,
                reason=(
                    f"utworzenie {index_path} + {len(index_preview_lines)} wpis(y)"
                    if index_created_now
                    else f"dopisanie {len(index_preview_lines)} wpis(y) do {index_path}"
                ),
                preview_lines=index_preview_lines,
                new_content=index_draft.current,
            )
        )

    moc_plans.sort(key=lambda p: p.path)
    return moc_plans + index_plans


class _FileDraft:
    """Wewnetrzny helper \u2014 trzyma tresc pliku w trakcie planowania.

    ``initial`` to tresc wyjsciowa (do porownania na koncu: czy byly zmiany),
    ``current`` to aktualna zywa kopia po ewentualnych modyfikacjach.
    """

    __slots__ = ("initial", "current")

    def __init__(self, initial: str, current: str | None = None) -> None:
        self.initial = initial
        self.current = current if current is not None else initial


def _infer_note_from_action(
    action: ProposedWrite, vault_manager: VaultManager
) -> VaultNote | None:
    """Buduje ``VaultNote`` na podstawie ``ProposedWrite.content`` + ``path``.

    - ``create`` / ``update`` \u2014 parsuje frontmatter z ``content``
    - ``append`` \u2014 jesli notatka juz istnieje, czyta z dysku (pomijamy
      w MOC update \u2014 zakladamy ze byla juz dolinkowana); jesli nie \u2014
      zwraca ``None`` (to blad semantyczny, ale zostawiamy decyzje
      executorowi).
    """

    if action.type == "append":
        if vault_manager.note_exists(action.path):
            return vault_manager.read_note(action.path)
        return None

    return _note_from_content(action.path, action.content)


def _note_from_content(rel_path: str, content: str) -> VaultNote:
    """Parsuje frontmatter z ``content`` \u2014 minimalnie, na potrzeby MOC/index."""

    match = _FRONTMATTER_RE.match(content) if content else None
    frontmatter: dict = {}
    if match:
        try:
            raw = yaml.safe_load(match.group(1))
            if isinstance(raw, dict):
                frontmatter = raw
        except yaml.YAMLError:
            frontmatter = {}

    tags_raw = frontmatter.get("tags") or []
    if isinstance(tags_raw, str):
        tags = [tags_raw.lstrip("#").strip()]
    elif isinstance(tags_raw, list):
        tags = [str(t).lstrip("#").strip() for t in tags_raw if str(t).strip()]
    else:
        tags = []

    parent_raw = frontmatter.get("parent")
    parent = _strip_wikilink(parent_raw) if parent_raw else None

    type_raw = frontmatter.get("type")
    note_type = str(type_raw).strip() if type_raw else None

    status_raw = frontmatter.get("status")
    status = str(status_raw).strip() if status_raw else None

    return VaultNote(
        path=rel_path,
        title=Path(rel_path).stem,
        content=content,
        frontmatter=frontmatter,
        tags=tags,
        type=note_type,
        parent=parent,
        related=[],
        status=status,
        wikilinks=[],
    )


def _strip_wikilink(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _WIKILINK_TARGET_RE.match(text)
    if not match:
        return text or None
    inner = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
    return inner or None


def _is_moc_note(note: VaultNote) -> bool:
    if (note.type or "").lower() == "moc":
        return True
    return Path(note.path).stem.startswith("MOC__")


def _looks_like_moc_path(path: str) -> bool:
    """Czy sciezka wyglada na plik MOC (heurystyka po stem).

    Uzywane do decyzji "model juz zadbal o ten MOC" w safety-netie
    Fazy 7 — jesli ``ProposedWrite.path`` to plik MOC, nie doklejamy
    nic post-hoc do tego samego pliku.
    """

    stem = Path(path).stem
    return stem.startswith("MOC__")


def _moc_contains_link(content: str, stem: str) -> bool:
    """Czy tresc MOC zawiera juz wikilink ``[[stem]]`` lub ``[[stem|...]]``."""

    target = re.escape(stem)
    pattern = re.compile(r"\[\[" + target + r"(\|[^\]]*)?(#[^\]]*)?\]\]")
    return bool(pattern.search(content))


def _append_line(existing: str, line: str) -> str:
    if not existing:
        return line + "\n"
    if existing.endswith("\n\n"):
        return existing + line + "\n"
    if existing.endswith("\n"):
        return existing + line + "\n"
    return existing + "\n\n" + line + "\n"
