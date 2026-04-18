"""``replace_section`` - podmienia tresc istniejacej sekcji (Faza 3).

**Semantyka:**

Znajduje sekcje po headingu (``## {heading}`` — dowolny poziom 2-6) i
podmienia jej body (bez linii headingu) na nowa tresc. Granica sekcji:
do pierwszej kolejnej sekcji o tym samym lub wyzszym poziomie (albo konca
pliku).

**Preconditions:**

- ``path`` przechodzi walidacje (relatywny, ``.md``)
- plik istnieje (realnie lub jako pending create)
- sekcja o podanym headingu istnieje (error "heading not found", uzyj
  ``append_section`` jesli chcesz dodac nowa)

**Kiedy uzywac:**

- Aktualizacja podsumowania "Zaleznosci" po zmianie w imports
- Przepisanie "Historia zmian" po rebase'ie
- Edycja "Kontekst" w ADR po otrzymaniu feedbacku

Nie do dodawania (append_section) ani do usuwania (daj pusty body, zostanie
sam heading).
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
    replace_section,
)


class _ReplaceSectionArgs(BaseModel):
    """Schemat argumentow ``replace_section``."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ...,
        min_length=1,
        description="Sciezka relatywna do istniejacej notatki .md.",
    )
    heading: str = Field(
        ...,
        min_length=1,
        description=(
            "Tytul istniejacej sekcji do podmienienia, bez znakow ``#``. "
            "Poziom (##, ###, ####) jest rozpoznawany automatycznie — dopasowujemy "
            "po nazwie. Sekcja MUSI juz istniec."
        ),
    )
    content: str = Field(
        ...,
        description=(
            "Nowa tresc sekcji (markdown, sam body bez linii headingu). Pusty "
            "string zachowuje sam heading. Do usuniecia calej sekcji uzyj "
            "'update_note' (na razie nie ma 'delete_section')."
        ),
    )


class ReplaceSectionTool(Tool):
    """Podmienia tresc istniejacej sekcji (proponuje - zapis po submit_plan)."""

    name = "replace_section"
    description = (
        "Podmienia tresc istniejacej sekcji (## heading) w notatce. Granica sekcji: "
        "do kolejnego headingu tego samego lub wyzszego poziomu. Jesli sekcja nie "
        "istnieje — zwroci blad; wtedy uzyj 'append_section'. Granulacja zamiast "
        "nadpisywania calej notatki. Nic nie zapisuje natychmiast - finalizacja "
        "przez submit_plan."
    )

    def input_schema(self) -> dict[str, Any]:
        return _ReplaceSectionArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _ReplaceSectionArgs.model_validate(args)
        except Exception as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        normalized = normalize_path_or_error(parsed.path)
        if isinstance(normalized, ToolResult):
            return normalized

        if not path_exists_effectively(ctx, normalized):
            return ToolResult(
                ok=False,
                error=f"path does not exist: {normalized!r}",
            )

        current = compute_effective_content(ctx, normalized)
        if current is None:
            return ToolResult(
                ok=False,
                error=f"Nie udalo sie odczytac biezacej tresci {normalized!r}.",
            )

        try:
            new_content = replace_section(
                current,
                heading=parsed.heading,
                new_body=parsed.content,
            )
        except MarkdownOpsError as exc:
            return map_markdown_error(self.name, exc, ctx, normalized)

        return register_granular_update(
            ctx=ctx,
            tool_name=self.name,
            normalized_path=normalized,
            new_content=new_content,
            op_summary=f"REPLACE_SECTION '{parsed.heading}'",
            extra_log_args={"heading": parsed.heading, "body_len": len(parsed.content)},
        )


__all__ = ["ReplaceSectionTool"]
