"""Wspolne helpery dla granulowanych narzedzi write (Faza 3 refaktoru).

**Problem ktory rozwiazujemy:**

Narzedzia granulowane (``append_section``, ``add_table_row``,
``add_moc_link``, ``update_frontmatter``, ...) modyfikuja male fragmenty
istniejacego pliku. Kazde z nich musi:

1. Miec "aktualny stan" pliku pod reka — wliczajac **wczesniejsze
   propozycje write** zarejestrowane w tej samej sesji (scenariusz:
   model wola ``add_moc_link`` dwa razy na MOC_Core pod rzad, drugi
   call musi widziec pierwszy wpis).
2. Zarejestrowac swoj wynik jako ``ProposedWrite`` zgodny z
   ``ctx.proposed_writes`` — zeby ``apply_pending`` i flow preview
   zostaly nietkniete.

**Strategia:**

- ``compute_effective_content(ctx, path)`` — replay pending writes na
  tresci z dysku (``create`` overwrite, ``update`` overwrite, ``append``
  dokleja).
- ``register_granular_update(...)`` — proba **koalescencji** z ostatnia
  propozycja na te sciezke. Jesli ostatnia byla ``create`` lub ``update``,
  podmieniamy jej tresc w miejscu (zamiast dodawac kolejna). Gdy ostatnia
  byla ``append`` albo nic — dodajemy nowa akcje ``update``.

Koalescencja ogranicza rozmiar ``proposed_writes`` w scenariuszach z
wieloma drobnymi zmianami pod rzad (``add_moc_link`` × 5) — zamiast
5 akcji ``update`` mamy jedna. W efekcie preview pokazuje jedna
zielona zmiane, a nie 5 nakladajacych sie na siebie.

**Czego NIE robi:**

- Nie waliduje sciezki (to ``normalize_path_or_error``).
- Nie sprawdza istnienia pliku (to robi kazde narzedzie w swoim
  kontekscie, bo semantyka bywa rozna — ``append_section`` wymaga
  istniejacego pliku, ale ``add_moc_link`` moze dzialac na swiezo
  proponowanym MOC-u).
- Nie rozumie semantyki markdown — to warstwa ``_markdown_ops``.
"""

from __future__ import annotations

import logging
from typing import Any

from src.agent.models_actions import ActionType, ProposedWrite
from src.agent.tools.base import ToolResult
from src.agent.tools.context import ToolExecutionContext
from src.agent.tools.vault_write._markdown_ops import MarkdownOpsError


logger = logging.getLogger(__name__)


def compute_effective_content(
    ctx: ToolExecutionContext,
    normalized_path: str,
) -> str | None:
    """Zwraca tresc pliku "tak, jakby wszystkie pending writes sie wykonaly".

    Pipeline:

    1. Start: tresc z dysku (``vault_manager.read_text``) jesli plik
       istnieje, inaczej ``None``.
    2. Iteracja po ``ctx.proposed_writes`` w kolejnosci rejestracji:

       - ``create`` → ``base = action.content`` (nadpisuje cokolwiek, co
         bylo — w praktyce create powinno byc pierwsze dla danej sciezki).
       - ``update`` → ``base = action.content``.
       - ``append`` → ``base = base + sep + action.content`` (separator
         dobrany jak w ``VaultManager.append``).

    3. Zwraca ``base`` albo ``None`` gdy plik nie istnial i nie bylo
       create/update w sesji.

    Idempotentne, bez side-effectow.
    """

    if not isinstance(normalized_path, str) or not normalized_path:
        raise ValueError("normalized_path musi byc niepustym stringiem")

    base: str | None = None
    try:
        if ctx.vault_manager.note_exists(normalized_path):
            base = ctx.vault_manager.read_text(normalized_path)
    except Exception:
        logger.exception(
            "compute_effective_content: nie udalo sie czytac %s z dysku", normalized_path
        )
        base = None

    for action in ctx.proposed_writes:
        if action.path != normalized_path:
            continue
        if action.type == "create":
            base = action.content
        elif action.type == "update":
            base = action.content
        elif action.type == "append":
            if base is None:
                base = action.content
            else:
                if base.endswith("\n\n"):
                    sep = ""
                elif base.endswith("\n"):
                    sep = "\n"
                else:
                    sep = "\n\n"
                base = base + sep + action.content
    return base


def register_granular_update(
    *,
    ctx: ToolExecutionContext,
    tool_name: str,
    normalized_path: str,
    new_content: str,
    op_summary: str,
    extra_log_args: dict[str, Any] | None = None,
) -> ToolResult:
    """Rejestruje wynik granulowanej zmiany jako ``ProposedWrite`` (z koalescencja).

    Koalescencja:

    - Jesli **ostatnia** propozycja write na te sciezke to ``create`` —
      podmieniamy jej ``content`` (pozostaje ``create``). Dzieki temu
      apply_pending zobaczy jeden create z finalna trescia (nie create
      + serie update'ow).
    - Jesli ostatnia to ``update`` — podmieniamy jej ``content`` (pozostaje
      ``update``). Analogicznie.
    - Jesli ostatnia to ``append`` — dodajemy nowa akcje ``update`` z
      ``new_content`` na koncu listy. apply_pending wykona najpierw
      append (na bazowej tresci), potem update (overwrite finalna).
    - Jesli nic wczesniej nie bylo dla tej sciezki — dodajemy nowa
      akcje ``update`` (plik musi istniec na dysku).

    :param op_summary: krotki opis operacji ("append section 'X'",
        "add_table_row to 'Decyzje'") — trafi do ``ctx.executed_actions``
        i do ``ToolResult.content`` jako komunikat dla modelu.
    :returns: ``ToolResult(ok=True, content="OP queued: ...")``.
    """

    if not isinstance(new_content, str):
        return ToolResult(
            ok=False, error=f"new_content musi byc stringiem, dostalismy {type(new_content).__name__}"
        )
    if new_content == "":
        return ToolResult(ok=False, error="new_content nie moze byc pusty")

    latest_idx: int | None = None
    for i in range(len(ctx.proposed_writes) - 1, -1, -1):
        if ctx.proposed_writes[i].path == normalized_path:
            latest_idx = i
            break

    action_type: ActionType = "update"
    if latest_idx is not None:
        latest = ctx.proposed_writes[latest_idx]
        if latest.type in ("create", "update"):
            action_type = latest.type
            ctx.proposed_writes[latest_idx] = ProposedWrite(
                type=action_type,
                path=normalized_path,
                content=new_content,
            )
        else:
            ctx.proposed_writes.append(
                ProposedWrite(type="update", path=normalized_path, content=new_content)
            )
    else:
        ctx.proposed_writes.append(
            ProposedWrite(type="update", path=normalized_path, content=new_content)
        )

    log_args: dict[str, Any] = {
        "path": normalized_path,
        "op": op_summary,
        "content_len": len(new_content),
        "action_type": action_type,
    }
    if extra_log_args:
        log_args.update(extra_log_args)

    ctx.record_action(
        tool=tool_name,
        path=normalized_path,
        args=log_args,
        ok=True,
    )
    ctx.invalidate_vault_knowledge()

    return ToolResult(
        ok=True,
        content=(
            f"{op_summary} queued for {normalized_path} "
            f"({len(new_content)} chars, action_type={action_type}). "
            f"Finalizacja w submit_plan."
        ),
    )


def map_markdown_error(tool_name: str, exc: MarkdownOpsError, ctx: ToolExecutionContext, normalized_path: str | None) -> ToolResult:
    """Mapuje ``MarkdownOpsError`` na ``ToolResult(ok=False, ...)`` + log.

    Celowy shortcut, zeby kazde granulowane narzedzie nie powtarzalo
    try/except + record_action. Model dostaje czytelny ``ERROR: ...``.
    """

    message = str(exc)
    ctx.record_action(
        tool=tool_name,
        path=normalized_path,
        args={"path": normalized_path},
        ok=False,
        error=message,
    )
    return ToolResult(ok=False, error=message)


__all__ = [
    "compute_effective_content",
    "map_markdown_error",
    "register_granular_update",
]
