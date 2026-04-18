"""``update_frontmatter`` - ustawia pojedyncze pole YAML frontmatter (Faza 3).

**Semantyka:**

Parsuje YAML frontmatter pliku, ustawia podane ``field = value``, serializuje
z powrotem. Body pliku pozostaje **nietkniete** (wazne dla pluginu
"Obsidian Git" i "Update modified date" — markery diff-view nie trafiaja
do frontmattera).

**Dla pol typu lista** (``tags``, ``related``) — zastepuje CALA liste.
Do dopisania pojedynczej pozycji do listy uzyj ``add_related_link`` albo
przyszlego ``add_tag`` (nie w Fazie 3).

**Kiedy uzywac:**

- Zbumpowanie ``updated: 2026-04-18`` po edycji notatki
- Zmiana ``status: draft`` → ``status: active``
- Ustawienie ``parent: "[[MOC___Architektura]]"`` dla swiezej notatki
- Przestawienie ``type`` (rzadkie, zazwyczaj type jest kontraktem)

**Preconditions:**

- ``path`` przechodzi walidacje (relatywny, ``.md``)
- plik istnieje (realnie lub jako pending create)
- frontmatter jest parsowalny YAML (inaczej blad, model musi naprawic
  osobnym ``update_note``)

**Brak frontmattera w pliku:** tworzymy nowy YAML frontmatter z jednym
polem ``field: value``. Stary body zostaje.

**Typ value:**

JSON types bez konstruktorow Python-specific. String / int / float / bool
/ list / dict / None. ``field: value`` zapisywane przez ``yaml.safe_dump``
— np. list Pythonowa zapisuje sie jako blok YAML ``- item1\\n- item2``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext
from src.agent.tools.vault_write._common import (
    normalize_path_or_error,
    path_exists_effectively,
)
from src.agent.tools.vault_write._granular import (
    compute_effective_content,
    map_markdown_error,
    register_granular_update,
)
from src.agent.tools.vault_write._markdown_ops import (
    MarkdownOpsError,
    set_frontmatter_field,
)


class _UpdateFrontmatterArgs(BaseModel):
    """Schemat argumentow ``update_frontmatter``."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ...,
        min_length=1,
        description="Sciezka relatywna do istniejacej notatki .md.",
    )
    field: str = Field(
        ...,
        min_length=1,
        description=(
            "Nazwa pola YAML frontmatter do ustawienia (np. 'updated', 'status', "
            "'parent', 'type'). Tworzy pole jesli nie istnialo."
        ),
    )
    value: Any = Field(
        ...,
        description=(
            "Nowa wartosc. Moze byc string ('active'), int, bool, lista "
            "(['tag1','tag2']), mapa. Dla list/map — zastepuje w calosci. "
            "Do dopisania jednego elementu do listy uzyj 'add_related_link'."
        ),
    )


class UpdateFrontmatterTool(Tool):
    """Ustawia pojedyncze pole YAML frontmatter (proponuje - zapis po submit_plan)."""

    name = "update_frontmatter"
    description = (
        "Parsuje YAML frontmatter pliku, ustawia pole=value, zapisuje z powrotem. "
        "Body pozostaje nietkniete. Dla list (tags, related) — zastepuje cala liste; "
        "do dopisania jednego wpisu uzyj 'add_related_link'. Nic nie zapisuje "
        "natychmiast - finalizacja przez submit_plan."
    )

    def input_schema(self) -> dict[str, Any]:
        return _UpdateFrontmatterArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _UpdateFrontmatterArgs.model_validate(args)
        except Exception as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        normalized = normalize_path_or_error(parsed.path)
        if isinstance(normalized, ToolResult):
            return normalized

        if not path_exists_effectively(ctx, normalized):
            return ToolResult(ok=False, error=f"path does not exist: {normalized!r}")

        current = compute_effective_content(ctx, normalized)
        if current is None:
            return ToolResult(
                ok=False,
                error=f"Nie udalo sie odczytac biezacej tresci {normalized!r}.",
            )

        try:
            new_content = set_frontmatter_field(current, parsed.field, parsed.value)
        except MarkdownOpsError as exc:
            return map_markdown_error(self.name, exc, ctx, normalized)

        return register_granular_update(
            ctx=ctx,
            tool_name=self.name,
            normalized_path=normalized,
            new_content=new_content,
            op_summary=f"UPDATE_FRONTMATTER {parsed.field}=...",
            extra_log_args={"field": parsed.field, "value_type": type(parsed.value).__name__},
        )


__all__ = ["UpdateFrontmatterTool"]
