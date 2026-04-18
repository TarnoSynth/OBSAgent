"""``add_moc_link`` - dopisuje wikilink do sekcji w MOC (Faza 3).

**Semantyka:**

Znajduje sekcje o podanym headingu w pliku MOC i dopisuje bullet
``- [[wikilink]] — description`` po ostatnim istniejacym bullecie w sekcji.
Idempotentne: drugi call z tym samym ``wikilink`` zwroci ``ok=True`` ale
bez dodawania (content w ``ToolResult.content`` informuje "already present").

**Preconditions:**

- ``moc_path`` przechodzi walidacje (relatywny, ``.md``)
- plik istnieje (realnie lub jako pending create)
- sekcja o podanym headingu istnieje w MOC

**Kiedy uzywac:**

- Po stworzeniu nowej notatki typu ``module`` dopisujemy ja do
  ``MOC__Core`` pod sekcja "Moduly".
- Po create notatki ADR → dopisanie do ``MOC__Architektura`` pod
  "Decyzje" (jesli MOC nie ma tabeli — inaczej uzyj ``add_table_row``).

**Format wikilinku:**

Akceptujemy ``"Auth"``, ``"[[Auth]]"``, ``"[[Auth|alias]]"``. Brakujace
``[[...]]`` dopisujemy sami. Alias (``|``) jest zachowany.

**Dlaczego osobne narzedzie a nie re-uzycie ``append_section``:**

Bullet-lista jest typowym wzorcem MOC. Dopisanie pojedynczego linku
to ~1 linijka, a ``append_section`` rezerwowalibysmy na cale sekcje.
Ponadto idempotencja (wikilink juz jest → no-op) wymaga porownywania
per bullet, nie per cala sekcja.
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
    add_bullet_link_under_heading,
)


class _AddMocLinkArgs(BaseModel):
    """Schemat argumentow ``add_moc_link``."""

    model_config = ConfigDict(extra="forbid")

    moc_path: str = Field(
        ...,
        min_length=1,
        description=(
            "Sciezka relatywna do pliku MOC (np. 'MOC___Architektura.md'). "
            "Plik musi juz istniec w vaulcie lub byc proponowany przez "
            "wczesniejszy create_note w tej sesji."
        ),
    )
    section: str = Field(
        ...,
        min_length=1,
        description=(
            "Tytul sekcji (bez ``#``), pod ktora dopisujemy bullet. Np. "
            "'Moduly', 'Decyzje', 'Dokumenty'. Sekcja MUSI istniec w MOC."
        ),
    )
    wikilink: str = Field(
        ...,
        min_length=1,
        description=(
            "Docelowy wikilink, np. 'Auth', '[[Auth]]' albo '[[Auth|Autoryzacja]]'. "
            "Brakujace ``[[...]]`` dopisujemy sami. Idempotentne — jesli ten "
            "wikilink juz jest w sekcji, nie duplikujemy."
        ),
    )
    description: str | None = Field(
        None,
        description=(
            "Opcjonalny krotki opis po znaku ``—``. Np. description='modul auth' da "
            "wpis '- [[Auth]] — modul auth'. Przy kolejnym wywolaniu z tym samym "
            "wikilinkiem description NIE jest aktualizowane (idempotencja)."
        ),
    )


class AddMocLinkTool(Tool):
    """Dopisuje wikilink do sekcji MOC (proponuje - zapis po submit_plan, idempotentne)."""

    name = "add_moc_link"
    description = (
        "Dopisuje bullet '- [[wikilink]] — description' pod sekcja w pliku MOC. "
        "Sekcja musi istniec. Idempotentne — drugi call z tym samym wikilinkiem "
        "nie duplikuje wpisu. Uzywaj po utworzeniu nowej notatki zeby zarejestrowac "
        "ja w MOC. Nic nie zapisuje natychmiast - finalizacja przez submit_plan."
    )

    def input_schema(self) -> dict[str, Any]:
        return _AddMocLinkArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _AddMocLinkArgs.model_validate(args)
        except Exception as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        normalized = normalize_path_or_error(parsed.moc_path)
        if isinstance(normalized, ToolResult):
            return normalized

        if not path_exists_effectively(ctx, normalized):
            return ToolResult(
                ok=False,
                error=(
                    f"MOC path does not exist: {normalized!r} - utworz MOC przez "
                    f"create_note zanim dopiszesz do niego link"
                ),
            )

        current = compute_effective_content(ctx, normalized)
        if current is None:
            return ToolResult(
                ok=False,
                error=f"Nie udalo sie odczytac biezacej tresci {normalized!r}.",
            )

        try:
            new_content, added = add_bullet_link_under_heading(
                current,
                heading=parsed.section,
                wikilink=parsed.wikilink,
                description=parsed.description,
            )
        except MarkdownOpsError as exc:
            return map_markdown_error(self.name, exc, ctx, normalized)

        if not added:
            ctx.record_action(
                tool=self.name,
                path=normalized,
                args={
                    "section": parsed.section,
                    "wikilink": parsed.wikilink,
                    "result": "noop_already_present",
                },
                ok=True,
            )
            return ToolResult(
                ok=True,
                content=(
                    f"Wikilink '{parsed.wikilink}' juz istnieje w sekcji "
                    f"'{parsed.section}' pliku {normalized!r} - no-op (idempotencja)."
                ),
            )

        return register_granular_update(
            ctx=ctx,
            tool_name=self.name,
            normalized_path=normalized,
            new_content=new_content,
            op_summary=f"ADD_MOC_LINK '{parsed.wikilink}' -> '{parsed.section}'",
            extra_log_args={
                "section": parsed.section,
                "wikilink": parsed.wikilink,
                "description_present": bool(parsed.description),
            },
        )


__all__ = ["AddMocLinkTool"]
