"""``create_note`` - tworzy nowa notatke (Faza 2 refaktoru agentic tool loop).

**Semantyka:**

Rejestruje ``ProposedWrite(type="create", path=..., content=...)`` w
``ctx.proposed_writes``. NIE zapisuje do vaulta bezposrednio - zapis
nastapi przez ``apply_pending`` po zakonczeniu calej sesji tool-use.

**Preconditions:**

- ``path`` przechodzi walidacje (relatywny, ``.md``, bez ``..``)
- sciezka **nie istnieje** w vaulcie (``vault_manager.note_exists`` == False)
- sciezka **nie byla juz zaproponowana** jako ``create`` w tej sesji
  (``ctx.has_pending_create`` == False)

Gdy ktorakolwiek regula nie przejdzie - ``ToolResult(ok=False, error=...)``.
Model dostaje czytelny blad i ma szanse sie poprawic (np. uzyc ``update_note``
zamiast ``create_note``).
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


class _CreateNoteArgs(BaseModel):
    """Schema argumentow ``create_note`` - zrodlo prawdy dla ``input_schema``.

    Pydantic generuje z tego JSON Schema, ktore trafia bezposrednio do
    providera LLM przez ``list_tools``. Opisy pol (``Field(description=...)``)
    sa widoczne dla modelu - pisze je w 3. osobie, krotko, w jezyku agenta (PL).
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ...,
        min_length=1,
        description=(
            "Sciezka relatywna wzgledem vaulta, np. 'Architektura/Auth.md'. "
            "Zawsze ``.md``, bez ``..``, bez ``/`` na poczatku, bez drive-letter."
        ),
    )
    content: str = Field(
        ...,
        min_length=1,
        description=(
            "Cala tresc notatki, zazwyczaj z frontmatterem YAML na poczatku "
            "(``---\\ntype: ...\\ntags: [...]\\n---``) plus tytul ``# ...`` i cialo. "
            "Dokladna forma wg szablonow z 'templates/'."
        ),
    )


class CreateNoteTool(Tool):
    """Tworzy nowa notatke typu ``doc`` (proponuje - zapis po submit_plan).

    Faza 7: **tylko dla ``type: doc``**. Dla innych typow (hub/concept/
    technology/decision/module/changelog) uzyj dedykowanego narzedzia.
    """

    name = "create_note"
    description = (
        "Proponuje utworzenie nowej notatki typu `doc` pod wskazana sciezka. "
        "**Tylko dla wolnych dokumentow (`type: doc` lub bez frontmattera)** - "
        "dla typow hub/concept/technology/decision/module/changelog uzyj "
        "dedykowanych narzedzi (create_hub / create_concept / create_technology / "
        "create_decision / create_module / create_changelog_entry). "
        "Sciezka musi byc unikalna w vaulcie. Tresc powinna zawierac frontmatter YAML "
        "(`type: doc`) i body. Nic nie zapisuje od razu - finalizacja przez submit_plan."
    )

    def input_schema(self) -> dict[str, Any]:
        return _CreateNoteArgs.model_json_schema()

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

        rejection = reject_specialized_type(tool_name=self.name, content=content)
        if rejection is not None:
            return rejection

        if path_exists_effectively(ctx, normalized):
            return ToolResult(
                ok=False,
                error=(
                    f"path exists: {normalized!r} - uzyj 'update_note' (nadpisanie) "
                    f"lub 'append_to_note' (dopisek)"
                ),
            )

        return build_and_register_action(
            ctx=ctx,
            tool_name=self.name,
            action_type="create",
            normalized_path=normalized,
            content=content,
        )


__all__ = ["CreateNoteTool"]
