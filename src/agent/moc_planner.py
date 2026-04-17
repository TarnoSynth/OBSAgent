"""Pre-compute co ``MOCManager.ensure_note_in_moc`` i ``update_index`` zrobia.

Agent **nigdy** nie commituje bez zgody usera. Preview musi pokazac
userowi **pelny** zestaw zmian, ktory znajdzie sie w commicie na vault
\u2014 nie tylko ``AgentAction`` od AI, ale takze efekty auto-utrzymania
MOC i indeksu. Inaczej user widzi polowe prawdy.

Ta warstwa wykonuje **suchy plan** (dry-run): na podstawie aktualnego
``VaultKnowledge`` i listy ``AgentAction`` generuje ``PlannedVaultWrite``
\u2014 konkretne operacje na plikach MOC i ``_index.md``. Nic nie zapisuje.

Executor (``action_executor.py``) dostaje ten plan razem z lista akcji
i aplikuje caloscii w odpowiedniej kolejnosci po akceptacji ``[T]``:

1. AgentAction (create/update/append)
2. PlannedVaultWrite dla MOC
3. PlannedVaultWrite dla ``_index.md``
4. Jeden commit Gitowy na vault

Dla akcji ``append`` **nie** aktualizujemy MOC (zalozenie: notatka juz
istniala, wiec juz jest w MOC lub celowo nie jest). Aktualizujemy
``_index.md`` tylko dla ``create`` \u2014 update/append nie zmienia obecnosci
wpisu w indeksie.

Algorytm dla wielu akcji tworzacych notatki w tym samym MOC / indeksie:
idziemy po akcjach kolejno i aktualizujemy **zywa kopie** tresci MOC /
index w pamieci. Na koncu emitujemy jeden ``PlannedVaultWrite`` per plik
(jesli byly zmiany). Dzieki temu nie trzeba doklejac
``append-over-append`` w executorze.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from src.agent.models_actions import AgentAction
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
    actions: list[AgentAction],
    vault_manager: VaultManager,
    knowledge: VaultKnowledge,
    *,
    index_path: str = DEFAULT_INDEX_PATH,
) -> list[PlannedVaultWrite]:
    """Generuje plan aktualizacji MOC i indeksu dla listy ``AgentAction``.

    Algorytm:

    1. Pobierz obecne tresci plikow MOC kandydatow + indeksu (w pamieci).
    2. Dla kazdej akcji (create/update) wywnioskuj ``VaultNote`` i wyznacz
       pasujacy MOC. Jesli jest i nie linkuje jeszcze do notatki \u2014
       dopisz ``- [[stem]]`` do zywej kopii.
    3. Dla kazdej akcji typu ``create`` dopisz wpis do indeksu (sekcja
       wedlug typu). Jesli indeks nie istnieje \u2014 zaczynamy od szablonu.
    4. Na koncu emitujemy po jednym ``PlannedVaultWrite`` per plik, dla
       ktorych finalna tresc roznie sie od poczatkowej.

    Zwraca: plany MOC (posortowane po sciezce) + plany index (max 1).
    """

    moc_manager = MOCManager(vault_manager)

    moc_state: dict[str, _FileDraft] = {}
    moc_preview: dict[str, list[str]] = {}

    index_draft: _FileDraft | None = None
    index_created_now = False
    index_preview_lines: list[str] = []

    for action in actions:
        if action.type not in ("create", "update", "append"):
            continue

        note = _infer_note_from_action(action, vault_manager)
        if note is None:
            continue

        if action.type in ("create", "update") and not _is_moc_note(note):
            moc = moc_manager.find_moc_for_note(note, knowledge=knowledge)
            if moc is not None:
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
            section = MOCManager._section_title_for(note)  # type: ignore[attr-defined]
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
    action: AgentAction, vault_manager: VaultManager
) -> VaultNote | None:
    """Buduje ``VaultNote`` na podstawie ``AgentAction.content`` + ``path``.

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
