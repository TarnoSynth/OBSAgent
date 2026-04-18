"""``create_concept`` — tworzy notatke typu concept (Faza 5 refaktoru).

Concept to **pojecie** — kluczowy termin, paradygmat, wzorzec. W odroznieniu
od ``technology`` (konkretny produkt z nazwa wlasna), concept jest
**abstrakcyjny** — ``Modularny_monolit``, ``Event_sourcing``.

Renderer (``src.agent.tools.renderers.concept``) wymusza strukturowane
sekcje: definicja (prolog), kontekst, opcjonalnie alternatywy odrzucone.

Pola ``parent`` moze wskazywac na hub LUB MOC — concept nie musi byc
bezposrednio pod MOC-iem (moze zyc pod hubem tematycznym).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext
from src.agent.tools.renderers.concept import ConceptAlternative, render_concept
from src.agent.tools.vault_write._common import (
    build_and_register_action,
    normalize_path_or_error,
    path_exists_effectively,
)

__all__ = ["CreateConceptTool"]


class _AlternativeArg(BaseModel):
    """Pojedyncza alternatywa odrzucona.

    Pola w snake_case, zgodnie z konwencja JSON Schema dla LLM.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        min_length=1,
        description="Nazwa alternatywy (tekst albo wikilink ``[[X]]``).",
    )
    reason: str = Field(
        ...,
        min_length=1,
        description="Jedno-dwuzdaniowe uzasadnienie, dlaczego odrzucona.",
    )


class _CreateConceptArgs(BaseModel):
    """Schemat argumentow narzedzia ``create_concept``."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ...,
        min_length=1,
        description=(
            "Sciezka relatywna do vaulta, ``.md``. Np. 'concepts/Modularny_monolit.md'."
        ),
    )
    title: str = Field(
        ...,
        min_length=1,
        description="Tytul konceptu — trafi do ``# title``.",
    )
    definition: str = Field(
        ...,
        min_length=1,
        description=(
            "1-3 zdania definicji konceptu (co to jest). Bez headingu — prolog "
            "pod tytulem."
        ),
    )
    context: str = Field(
        ...,
        min_length=1,
        description=(
            "Body sekcji '## Kontekst' — gdzie/kiedy stosujemy, dlaczego istotne "
            "dla tego projektu. 2-6 zdan albo lista."
        ),
    )
    parent: str = Field(
        ...,
        min_length=1,
        description=(
            "Wikilink do rodzica. MOC (``MOC___Kompendium``) LUB hub tematyczny "
            "(``Architektura_systemu``). Concept MUSI miec parent zeby zyc pod "
            "odpowiednim wezlem grafu."
        ),
    )
    related: list[str] | None = Field(
        default=None,
        description="Lista wikilinkow related we frontmatterze.",
    )
    tags: list[str] | None = Field(
        default=None,
        description="Tagi dodatkowe (poza automatycznym ``concept``).",
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
        description="``active`` / ``draft`` / ``archived``. Domyslnie ``active``.",
    )
    alternatives: list[_AlternativeArg] | None = Field(
        default=None,
        description=(
            "Alternatywy odrzucone — renderowane jako tabela "
            "``| Alternatywa | Dlaczego odrzucona |``. Pusta lub ``None`` → "
            "sekcja pominieta."
        ),
    )


class CreateConceptTool(Tool):
    """Tworzy notatke typu ``concept`` (pojecie z kontekstem uzycia)."""

    name = "create_concept"
    description = (
        "Tworzy notatke typu 'concept' — pojecie/paradygmat/wzorzec (np. "
        "'Modularny monolit', 'Event sourcing'). Strukturowane pola: definicja, "
        "kontekst, opcjonalnie alternatywy odrzucone. Wymaga parenta (MOC lub hub). "
        "Renderer sklada markdown deterministycznie. Finalizacja przez submit_plan."
    )

    def input_schema(self) -> dict[str, Any]:
        return _CreateConceptArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _CreateConceptArgs.model_validate(args)
        except ValidationError as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        normalized_path = normalize_path_or_error(parsed.path)
        if isinstance(normalized_path, ToolResult):
            return normalized_path

        if path_exists_effectively(ctx, normalized_path):
            return ToolResult(
                ok=False,
                error=(
                    f"path exists: {normalized_path!r} — concept o tej sciezce juz istnieje."
                ),
            )

        alternatives = None
        if parsed.alternatives:
            alternatives = [
                ConceptAlternative(name=a.name, reason=a.reason) for a in parsed.alternatives
            ]

        content = render_concept(
            title=parsed.title,
            definition=parsed.definition,
            context=parsed.context,
            parent=parsed.parent,
            related=parsed.related or None,
            tags=parsed.tags or None,
            created=parsed.created,
            updated=parsed.updated,
            status=parsed.status,
            alternatives=alternatives,
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
                    f"CONCEPT created: title={parsed.title!r}, parent={parsed.parent!r}, "
                    f"alternatives={len(parsed.alternatives) if parsed.alternatives else 0}."
                ),
            )
        return result
