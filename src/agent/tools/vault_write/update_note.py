"""``update_note`` - pelne nadpisanie istniejacej notatki (Faza 2 refaktoru).

**Semantyka:**

Rejestruje ``ProposedWrite(type="update", ...)``. Po zakonczeniu sesji
``apply_pending`` zapisze nowa tresc, a poprzednia wersja zostanie
otoczona czerwonym callout'em ``[!failure]+`` w diff-view.

**Preconditions:**

- ``path`` przechodzi walidacje (relatywny, ``.md``)
- sciezka **istnieje** - realnie w vaulcie lub jako pending create
  (``path_exists_effectively`` obejmuje obie zaleznosci)
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext
from src.agent.tools.vault_write._common import (
    build_and_register_action,
    normalize_path_or_error,
    path_exists_effectively,
    reject_specialized_type,
    resolve_action_args,
)


class _UpdateNoteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ...,
        min_length=1,
        description=(
            "Sciezka relatywna do notatki, ktora ma byc nadpisana. Plik musi juz "
            "istniec w vaulcie (albo byc wczesniej utworzony przez create_note w tej sesji)."
        ),
    )
    content: str = Field(
        ...,
        min_length=1,
        description=(
            "Cala nowa tresc notatki - razem z frontmatterem. Zastapi w CALOSCI "
            "poprzednia tresc. Jesli chcesz DOPISAC fragment na koncu istniejacej "
            "notatki, uzyj 'append_to_note'."
        ),
    )


class UpdateNoteTool(Tool):
    """Nadpisuje pelna tresc istniejacej notatki typu ``doc`` (proponuje).

    Faza 7: **tylko dla ``type: doc``**. Dla innych typow uzywaj
    granularnych narzedzi (``append_section`` / ``replace_section`` /
    ``add_table_row`` / ``update_frontmatter`` / ``add_moc_link`` /
    ``add_related_link``). Generyczny full-rewrite nie jest akceptowany
    dla typow specjalizowanych.
    """

    name = "update_note"
    description = (
        "Proponuje nadpisanie istniejacej notatki typu `doc` pelna nowa trescia. "
        "**Tylko dla wolnych dokumentow (`type: doc` lub bez frontmattera)** - "
        "dla hub/concept/technology/decision/module/changelog uzyj granularnych narzedzi: "
        "append_section, replace_section, add_table_row, update_frontmatter, add_moc_link, "
        "add_related_link. "
        "Plik musi juz istniec w vaulcie. Do samego dopisku uzyj append_to_note. "
        "Nic nie zapisuje od razu - finalizacja przez submit_plan."
    )

    def input_schema(self) -> dict[str, Any]:
        return _UpdateNoteArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        resolved = resolve_action_args(args)
        if isinstance(resolved, ToolResult):
            return resolved
        raw_path, content = resolved

        normalized = normalize_path_or_error(raw_path)
        if isinstance(normalized, ToolResult):
            return normalized

        if not path_exists_effectively(ctx, normalized):
            return ToolResult(
                ok=False,
                error=(
                    f"path does not exist: {normalized!r} - "
                    f"uzyj 'create_note' zeby utworzyc nowa notatke"
                ),
            )

        rejection = reject_specialized_type(tool_name=self.name, content=content)
        if rejection is not None:
            return rejection

        return build_and_register_action(
            ctx=ctx,
            tool_name=self.name,
            action_type="update",
            normalized_path=normalized,
            content=content,
        )


__all__ = ["UpdateNoteTool"]
