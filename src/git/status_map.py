"""Mapowanie surowych statusów z wyjścia Gita na ChangeType."""

from __future__ import annotations

from src.git.models import ChangeType


def map_git_status(raw_status: str) -> ChangeType:
    """Mapuje status Gita na enum.

    Obsługuje:
    - klasyczne kody jednoliterowe (`A`, `M`, `D`, ...)
    - rename/copy z wynikiem podobieństwa (`R100`, `C085`)
    - statusy `git status --porcelain` (` M`, `MM`, `??`, `!!`)
    """

    status = raw_status.strip()
    if not status:
        raise ValueError("Pusty status Gita nie moze zostac zmapowany.")

    if status in {ChangeType.UNTRACKED.value, ChangeType.IGNORED.value}:
        return ChangeType(status)

    if len(status) > 1 and status[:1] in {ChangeType.RENAMED.value, ChangeType.COPIED.value}:
        return ChangeType(status[:1])

    if len(raw_status) == 2:
        for symbol in raw_status:
            if symbol == " ":
                continue
            if symbol in ChangeType._value2member_map_:
                return ChangeType(symbol)

    if status[:1] in ChangeType._value2member_map_:
        return ChangeType(status[:1])

    raise ValueError(f"Nieobslugiwany status Gita: {raw_status!r}")
