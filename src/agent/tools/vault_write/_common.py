"""Wspolna logika dla narzedzi write (Faza 2 refaktoru agentic tool loop).

Trzy narzedzia (``create_note`` / ``update_note`` / ``append_to_note``) robia
prawie to samo: walidacja sciezki → walidacja preconditions → rejestracja
``ProposedWrite`` w ``ctx.proposed_writes``. Roznia sie wylacznie regulami
preconditions i komunikatami bledow — reszte wydzielilismy tutaj, zeby kazde
narzedzie bylo malym plikiem-koordynatorem.

**Dlaczego ``ProposedWrite`` budujemy tutaj, nie w agencie:**

Walidacja Pydantic ``ProposedWrite`` (regex sciezki, niepusty content) juz
robi za nas polowe roboty. Gdy tool skonstruuje ``ProposedWrite`` i ten sie
nie zwalidowal — zwracamy ``ToolResult(ok=False, error=...)`` i model ma
szanse poprawic sie w nastepnej iteracji. Gdyby walidacja byla po stronie
agenta, blad wyszedlby dopiero na ``apply_pending`` — za pozno, bo sesja
juz zakonczona.
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from pydantic import ValidationError

from src.agent.models_actions import ActionType, ProposedWrite
from src.agent.tools.base import ToolResult
from src.agent.tools.context import ToolExecutionContext
from src.agent.vault_operations import InvalidPathError, validate_relative_md_path


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)

# Faza 7: specjalizowane typy notatek maja swoje dedykowane tool'e.
# ``create_note`` / ``update_note`` sa zarezerwowane **wylacznie** dla
# ``type: doc`` (lub notatek bez frontmattera / bez pola ``type``).
# Proba uzycia generycznego narzedzia dla innych typow konczy sie bledem
# z sugestia dedykowanego narzedzia.
_SPECIALIZED_TYPE_TO_TOOL: dict[str, str] = {
    "hub": "create_hub",
    "concept": "create_concept",
    "technology": "create_technology",
    "decision": "create_decision",
    "module": "create_module",
    "changelog": "create_changelog_entry",
}


def extract_note_type(content: str) -> str | None:
    """Wyciaga wartosc pola ``type`` z frontmattera YAML, jesli jest.

    Zwraca znormalizowany (lower-case, strip) string albo ``None``, jesli:

    - content nie zaczyna sie od frontmattera,
    - YAML sie nie parsuje,
    - pole ``type`` nie istnieje albo jest puste.

    Zadnych wyjatkow — funkcja jest best-effort (np. dla appenda, ktory
    nie ma frontmattera, zwraca ``None`` bez bledu).
    """

    if not content:
        return None
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("type")
    if raw is None:
        return None
    value = str(raw).strip().lower()
    return value or None


def reject_specialized_type(
    *,
    tool_name: str,
    content: str,
) -> ToolResult | None:
    """Zwraca ``ToolResult(ok=False, ...)`` jesli content nalezy do typu,
    dla ktorego istnieje dedykowane narzedzie. Inaczej ``None``.

    Wymuszenie konwencji z Fazy 7: ``create_note`` / ``update_note`` sa
    tylko dla ``type: doc``. Dla ``hub`` / ``concept`` / ``technology`` /
    ``decision`` / ``module`` / ``changelog`` — uzywaj dedykowanych
    narzedzi (domain creators maja silnie typowane argumenty, rendery
    i automatyczne dopisanie MOC/ADR-tabel).
    """

    note_type = extract_note_type(content)
    if note_type is None or note_type == "doc":
        return None
    dedicated = _SPECIALIZED_TYPE_TO_TOOL.get(note_type)
    if dedicated is None:
        return None
    return ToolResult(
        ok=False,
        error=(
            f"{tool_name} nie obsluguje notatek typu {note_type!r} - "
            f"uzyj dedykowanego narzedzia: {dedicated}. "
            f"Generyczne create_note/update_note sa tylko dla type: doc."
        ),
    )


def resolve_action_args(raw_args: dict[str, Any]) -> tuple[str, str] | ToolResult:
    """Wyciaga ``path`` + ``content`` z surowych argumentow JSON.

    Zwraca tuple ``(path, content)`` przy sukcesie albo ``ToolResult(ok=False)``
    z bledem (brak pola, zly typ). Wszystkie trzy tools write przyjmuja te same
    dwa pola — ta funkcja jest deduplikacja.
    """

    path = raw_args.get("path")
    content = raw_args.get("content")

    if not isinstance(path, str) or not path:
        return ToolResult(
            ok=False,
            error="argument 'path' jest wymagany i musi byc niepustym stringiem",
        )
    if not isinstance(content, str):
        return ToolResult(
            ok=False,
            error="argument 'content' jest wymagany i musi byc stringiem",
        )
    return path, content


def path_exists_effectively(ctx: ToolExecutionContext, normalized_path: str) -> bool:
    """Sprawdza, czy sciezka "istnieje" z punktu widzenia biezacej sesji tool-use.

    Zwraca True jesli:

    - plik realnie istnieje w vaulcie (``vault_manager.note_exists``)
    - LUB w tej sesji zarejestrowano juz ``create`` na tej sciezce
      (``ctx.has_pending_create``)

    Druga reguła rozwiazuje scenariusz sekwencyjny ``create_note("a.md")``
    w turze 1 → ``append_to_note("a.md")`` w turze 2. Realnie plik jeszcze
    nie istnieje (apply_pending leci po pętli), ale logicznie zostal juz
    zaproponowany.
    """

    if ctx.vault_manager.note_exists(normalized_path):
        return True
    return ctx.has_pending_create(normalized_path)


def build_and_register_action(
    *,
    ctx: ToolExecutionContext,
    tool_name: str,
    action_type: ActionType,
    normalized_path: str,
    content: str,
) -> ToolResult:
    """Buduje ``ProposedWrite``, rejestruje w contextie, zwraca ``ToolResult``.

    Walidacja Pydantic ``ProposedWrite`` robi drugie przejscie nad ta sama
    sciezka — to OK, daje spojny komunikat bledu dla modelu nawet jak
    skad innad (np. przez ``mcp-inspector``) przylecialy argumenty omijajace
    ``validate_relative_md_path``.

    **Effekty uboczne:**

    1. ``ctx.proposed_writes.append(action)``
    2. ``ctx.record_action(...)`` — do audit logu sesji
    3. ``ctx.invalidate_vault_knowledge()`` — kolejne reads widzą "swiezy stan"
       (technicznie vault nie sie zmienil, ale semantyka sesji jest taka ze
       po ``create`` cache jest niewazny; narzedzia eksploracyjne w Fazach 3+
       moga chciec widziec pending creates)

    :returns: ``ToolResult(ok=True, content="...")`` z krotkim potwierdzeniem
        do modelu, albo ``ok=False`` jesli Pydantic odrzucil argumenty.
    """

    try:
        action = ProposedWrite(type=action_type, path=normalized_path, content=content)
    except ValidationError as exc:
        ctx.record_action(
            tool=tool_name,
            path=normalized_path,
            args={"type": action_type, "path": normalized_path},
            ok=False,
            error=str(exc),
        )
        return ToolResult(
            ok=False,
            error=f"ProposedWrite validation failed: {exc}",
        )

    ctx.record_proposed_write(action)
    ctx.record_action(
        tool=tool_name,
        path=normalized_path,
        args={"type": action_type, "path": normalized_path, "content_len": len(content)},
        ok=True,
    )
    ctx.invalidate_vault_knowledge()

    summary = _summarize_success(action_type, normalized_path, content)
    return ToolResult(ok=True, content=summary)


def normalize_path_or_error(path: str) -> str | ToolResult:
    """Waliduje i normalizuje sciezke; blad zwraca jako ``ToolResult(ok=False)``."""

    try:
        return validate_relative_md_path(path)
    except InvalidPathError as exc:
        return ToolResult(ok=False, error=f"invalid path: {exc}")


def _summarize_success(action_type: ActionType, path: str, content: str) -> str:
    """Zbuduj krotka, przewidywalna odpowiedz dla modelu — 1 linia.

    Model dostanie to jako ``tool_result.content``. Format jest zachowany
    identyczny miedzy tools, zeby prompt caching widzial stabilny szum:
    ``"{VERB} queued for {path} ({N} chars)"``.
    """

    verb = {"create": "CREATE", "update": "UPDATE", "append": "APPEND"}[action_type]
    return f"{verb} queued for {path} ({len(content)} chars). Finalizacja w submit_plan."


__all__ = [
    "build_and_register_action",
    "extract_note_type",
    "normalize_path_or_error",
    "path_exists_effectively",
    "reject_specialized_type",
    "resolve_action_args",
]
