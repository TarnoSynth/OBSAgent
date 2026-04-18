"""``append_to_note`` - dopisek do istniejacej notatki (Faza 2 refaktoru).

**Semantyka:**

Rejestruje ``ProposedWrite(type="append", ...)``. Po zakonczeniu sesji
``apply_pending`` doklei tresc na koncu pliku (ze sensownym separatorem -
``VaultManager.append`` normalizuje ``\\n\\n``) i otoczy doklejke zielonym
callout'em ``[!tip]+`` w diff-view.

**Preconditions:**

- ``path`` przechodzi walidacje (relatywny, ``.md``)
- sciezka **istnieje** (realnie lub jako pending create)
- ``content`` NIE zawiera frontmattera - to byl by zly dopisek

Walidacja frontmattera dopisku jest miekka (checkujemy tylko ``content.startswith("---")``
jako heurystyke) - precyzyjnym arbiterem jest user w preview.
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
    resolve_action_args,
)


class _AppendToNoteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ...,
        min_length=1,
        description=(
            "Sciezka relatywna do notatki, do ktorej dopisujemy. Musi istniec "
            "(realnie albo wczesniej utworzona przez create_note w tej sesji)."
        ),
    )
    content: str = Field(
        ...,
        min_length=1,
        description=(
            "Fragment do dopisania na koncu notatki. Sam body (bez frontmattera YAML). "
            "Separator (`\\n\\n`) zostanie dobrany automatycznie. Do nadpisania calej "
            "tresci uzyj 'update_note' zamiast tego."
        ),
    )


class AppendToNoteTool(Tool):
    """Dopisuje tresc na koncu istniejacej notatki (proponuje - zapis po submit_plan)."""

    name = "append_to_note"
    description = (
        "Proponuje dopisanie fragmentu na koncu istniejacej notatki .md. "
        "Plik musi juz istniec w vaulcie (albo byc wczesniej proponowany przez create_note). "
        "Fragment to sam body bez frontmattera. Do zastapienia calej notatki uzyj 'update_note'. "
        "Nic nie zapisuje natychmiast - finalizacja przez submit_plan."
    )

    def input_schema(self) -> dict[str, Any]:
        return _AppendToNoteArgs.model_json_schema()

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

        if content.lstrip().startswith("---"):
            return ToolResult(
                ok=False,
                error=(
                    "content dla append_to_note nie moze zaczynac sie od '---' "
                    "(frontmatter YAML). Dopisek to samo body. Do zastapienia frontmattera "
                    "uzyj 'update_note'."
                ),
            )

        return build_and_register_action(
            ctx=ctx,
            tool_name=self.name,
            action_type="append",
            normalized_path=normalized,
            content=content,
        )


__all__ = ["AppendToNoteTool"]
