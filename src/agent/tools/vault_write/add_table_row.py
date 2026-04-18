"""``add_table_row`` - dopisuje wiersz do tabeli markdown pod sekcja (Faza 3).

**Semantyka:**

Znajduje pierwsza tabele (GFM ``|col|col|\\n|---|---|``) w sekcji o
podanym headingu i dopisuje wiersz na koncu tabeli. Liczba komorek MUSI
pasowac do naglowka tabeli.

**Preconditions:**

- ``path`` przechodzi walidacje (relatywny, ``.md``)
- plik istnieje (realnie lub jako pending create)
- sekcja o podanym headingu istnieje
- pod sekcja istnieje tabela markdown (header + separator ``|---|---|``)
- arity ``cells`` == arity naglowka tabeli

**Kiedy uzywac:**

- Dopisanie ADR do tabeli "Decyzje architektoniczne" w hubie
- Dodanie modulu do "Lista modulow" w MOC
- Dopisanie wiersza zmian w changelogu ze strukturowana tabela

Dla edycji pojedynczej komorki (nie dodawanie) - nie ma narzedzia w Fazie 3,
uzyj ``replace_section`` z pelnym nowym body sekcji.

**Pipe w komorkach:**

Znak ``|`` wewnatrz komorki jest automatycznie escapowany na ``\\|`` —
GFM wymaga tego zeby renderer nie potraktowal ``|`` jako separatora.
Model moze przekazac ``|`` jawnie, nie musi sam escape'owac.
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
    add_table_row,
)


class _AddTableRowArgs(BaseModel):
    """Schemat argumentow ``add_table_row``."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ...,
        min_length=1,
        description="Sciezka relatywna do notatki .md z tabela.",
    )
    table_heading: str = Field(
        ...,
        min_length=1,
        description=(
            "Tytul sekcji (bez ``#``), pod ktora znajduje sie tabela. Np. "
            "'Decyzje architektoniczne' albo 'Lista modulow'. Dopasowujemy "
            "pierwsza tabele pod tym headingiem."
        ),
    )
    cells: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Lista wartosci komorek dla nowego wiersza, w kolejnosci kolumn tabeli. "
            "Liczba elementow MUSI pasowac do naglowka tabeli — inaczej zwrocimy "
            "blad z aktualnym naglowkiem. Znak ``|`` zostanie automatycznie "
            "escapowany."
        ),
    )


class AddTableRowTool(Tool):
    """Dopisuje wiersz do tabeli markdown pod sekcja (proponuje - zapis po submit_plan)."""

    name = "add_table_row"
    description = (
        "Dopisuje jeden wiersz do pierwszej tabeli markdown pod sekcja o podanym "
        "headingu. Liczba komorek musi pasowac do naglowka tabeli (inaczej blad "
        "z naglowkiem). Uzywaj do dopisania ADR do tabeli 'Decyzje' w hubie, "
        "modulu do 'Lista modulow' itp. Nic nie zapisuje natychmiast - finalizacja "
        "przez submit_plan."
    )

    def input_schema(self) -> dict[str, Any]:
        return _AddTableRowArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _AddTableRowArgs.model_validate(args)
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
            new_content = add_table_row(current, parsed.table_heading, parsed.cells)
        except MarkdownOpsError as exc:
            return map_markdown_error(self.name, exc, ctx, normalized)

        return register_granular_update(
            ctx=ctx,
            tool_name=self.name,
            normalized_path=normalized,
            new_content=new_content,
            op_summary=f"ADD_TABLE_ROW to '{parsed.table_heading}'",
            extra_log_args={
                "table_heading": parsed.table_heading,
                "cols": len(parsed.cells),
            },
        )


__all__ = ["AddTableRowTool"]
