"""``create_module`` — tworzy notatke typu module (Faza 5 refaktoru).

Module to dokumentacja pojedynczego **modulu kodu** — pakietu / serwisu /
istotnego komponentu systemu. W odroznieniu od ``hub`` (agregator
obszaru), module to **pojedynczy wezel** dokumentacyjny jednego
fragmentu kodu.

Renderer (``src.agent.tools.renderers.module``) wymusza stala strukture:
streszczenie + odpowiedzialnosci + kluczowe elementy (tabela) + zaleznosci
+ opcjonalnie kontrakty/API + decyzje architektoniczne.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext
from src.agent.tools.renderers.module import ModuleElement, render_module
from src.agent.tools.vault_write._common import (
    build_and_register_action,
    normalize_path_or_error,
    path_exists_effectively,
)

__all__ = ["CreateModuleTool"]


class _ElementArg(BaseModel):
    """Wpis tabeli 'Kluczowe elementy'."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        min_length=1,
        description=(
            "Nazwa klasy/funkcji/endpointu (preferencja: w backtickach, np. ``Agent``)."
        ),
    )
    description: str = Field(
        ...,
        min_length=1,
        description="Jedno-dwuzdaniowy opis co ten element robi.",
    )


class _CreateModuleArgs(BaseModel):
    """Schemat argumentow ``create_module``."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ...,
        min_length=1,
        description="Sciezka relatywna ``.md`` (np. 'modules/Auth.md').",
    )
    title: str = Field(
        ...,
        min_length=1,
        description="Nazwa modulu — trafi do ``# title``.",
    )
    responsibility_summary: str = Field(
        ...,
        min_length=1,
        description=(
            "Jedno zdanie pod tytulem: 'Odpowiada za X. Zalezy od [[Y]].'. Bez headingu."
        ),
    )
    responsibilities: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Lista bulletow dla sekcji '## Odpowiedzialnosc'. Co modul robi, co "
            "NIE robi. Konkretnie, 2-5 punktow."
        ),
    )
    key_elements: list[_ElementArg] = Field(
        ...,
        min_length=1,
        description=(
            "Lista wpisow tabeli '## Kluczowe elementy' — klasy/funkcje/endpointy "
            "i ich role. Format: ``| Element | Opis |``."
        ),
    )
    uses: list[str] = Field(
        default_factory=list,
        description=(
            "Moduly/technologie z ktorych TEN modul korzysta (wikilinki albo "
            "gole nazwy — renderer owinie w ``[[...]]``)."
        ),
    )
    used_by: list[str] = Field(
        default_factory=list,
        description="Moduly ktore korzystaja z TEGO — wikilinki.",
    )
    parent: str = Field(
        ...,
        min_length=1,
        description=(
            "Wikilink do rodzicielskiego MOC-a lub huba. Module MUSI miec parent."
        ),
    )
    related: list[str] | None = Field(
        default=None,
        description="Lista wikilinkow related (do frontmattera).",
    )
    tags: list[str] | None = Field(
        default=None,
        description="Tagi dodatkowe (``module`` dodawany automatycznie).",
    )
    created: str = Field(
        ...,
        min_length=10,
        max_length=10,
        description="Data commita projektowego, ``YYYY-MM-DD``.",
    )
    updated: str | None = Field(
        default=None,
        description="Data ``YYYY-MM-DD``; ``None`` = kopia ``created``.",
    )
    status: str | None = Field(
        default=None,
        description="``active`` / ``deprecated`` / ``draft``. Default ``active``.",
    )
    contracts_api: str | None = Field(
        default=None,
        description=(
            "Opcjonalny body sekcji '## Kontrakty / API' — sygnatury / endpointy / "
            "ksztalty danych (markdown). ``None`` albo pusty → sekcja pominieta."
        ),
    )
    decisions: list[str] | None = Field(
        default=None,
        description=(
            "Opcjonalna lista wikilinkow do ADR/decision notes dla sekcji "
            "'## Decyzje architektoniczne'."
        ),
    )


class CreateModuleTool(Tool):
    """Tworzy notatke typu ``module`` (dokumentacja modulu kodu)."""

    name = "create_module"
    description = (
        "Tworzy notatke typu 'module' — dokumentacja jednego modulu kodu "
        "(pakiet/serwis/komponent). Strukturowane sekcje: odpowiedzialnosc, "
        "kluczowe elementy (tabela), zaleznosci (uses/used_by), opcjonalnie "
        "kontrakty_api i decyzje architektoniczne. Finalizacja przez submit_plan."
    )

    def input_schema(self) -> dict[str, Any]:
        return _CreateModuleArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _CreateModuleArgs.model_validate(args)
        except ValidationError as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        normalized_path = normalize_path_or_error(parsed.path)
        if isinstance(normalized_path, ToolResult):
            return normalized_path

        if path_exists_effectively(ctx, normalized_path):
            return ToolResult(
                ok=False,
                error=(
                    f"path exists: {normalized_path!r} — module o tej sciezce juz istnieje. "
                    "Uzyj 'update_note' albo granulowanych narzedzi zamiast 'create_module'."
                ),
            )

        elements = [
            ModuleElement(name=e.name, description=e.description) for e in parsed.key_elements
        ]

        content = render_module(
            title=parsed.title,
            responsibility_summary=parsed.responsibility_summary,
            responsibilities=list(parsed.responsibilities),
            key_elements=elements,
            uses=list(parsed.uses or []),
            used_by=list(parsed.used_by or []),
            parent=parsed.parent,
            related=parsed.related or None,
            tags=parsed.tags or None,
            created=parsed.created,
            updated=parsed.updated,
            status=parsed.status,
            contracts_api=parsed.contracts_api,
            decisions=parsed.decisions or None,
        )

        result = build_and_register_action(
            ctx=ctx,
            tool_name=self.name,
            action_type="create",
            normalized_path=normalized_path,
            content=content,
        )
        if result.ok:
            result = ToolResult(
                ok=True,
                content=(
                    f"{result.content}\n"
                    f"MODULE created: title={parsed.title!r}, parent={parsed.parent!r}, "
                    f"elements={len(parsed.key_elements)}."
                ),
            )
        return result
