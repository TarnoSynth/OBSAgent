"""``create_technology`` — tworzy notatke typu technology (Faza 5 refaktoru).

Technology to konkretna **biblioteka / serwis / platforma** uzywana w
systemie (``Qdrant``, ``PostgreSQL``, ``FastAPI``). Notatka sluzy jako
"karta technologii" — do czego u nas sluzy, gdzie jest zainstalowana,
jakie sa alternatywy.

**Wymagane pole ``role``:**

W frontmatterze notatki technology MUSI pojawic sie pole ``role`` —
jedno-zdaniowy opis "do czego u nas sluzy". Dzieki temu Dataview / skrypty
zewnetrzne moga wygenerowac "index technologii" jedna kwerenda bez
czytania body kazdej notatki. ``ConsistencyReport`` w Fazie 5 oznacza
brak ``role`` jako ``missing_required_fields``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext
from src.agent.tools.renderers.concept import ConceptAlternative
from src.agent.tools.renderers.technology import TechnologyLink, render_technology
from src.agent.tools.vault_write._common import (
    build_and_register_action,
    normalize_path_or_error,
    path_exists_effectively,
)

__all__ = ["CreateTechnologyTool"]


class _AlternativeArg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="Nazwa alternatywnej technologii.")
    reason: str = Field(..., min_length=1, description="Uzasadnienie odrzucenia.")


class _LinkArg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        ...,
        min_length=1,
        description="Krotki opis linku (np. 'Dokumentacja oficjalna').",
    )
    url: str = Field(
        ...,
        min_length=1,
        description="URL albo wikilink ``[[X]]`` wewnatrz vaulta.",
    )


class _CreateTechnologyArgs(BaseModel):
    """Schemat argumentow narzedzia ``create_technology``."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ...,
        min_length=1,
        description="Sciezka relatywna ``.md`` (np. 'technologies/Qdrant.md').",
    )
    title: str = Field(..., min_length=1, description="Nazwa technologii (np. 'Qdrant').")
    role: str = Field(
        ...,
        min_length=1,
        description=(
            "Jedno zdanie: 'do czego jej uzywamy'. Trafi ROWNIEZ do frontmattera "
            "(pole ``role``) — wymagane przez ConsistencyReport."
        ),
    )
    used_for: str = Field(
        ...,
        min_length=1,
        description=(
            "Body sekcji '## Do czego uzywamy' — 2-6 zdan albo lista. Gdzie w "
            "systemie jest uzyta, w jakich modulach, jakie problemy rozwiazuje."
        ),
    )
    parent: str = Field(
        ...,
        min_length=1,
        description=(
            "Wikilink do rodzica — zwykle hub 'Infrastruktura' / 'Architektura' "
            "albo MOC techniczny. Technology MUSI miec parent."
        ),
    )
    related: list[str] | None = Field(
        default=None,
        description="Lista wikilinkow do pola ``related`` frontmattera.",
    )
    tags: list[str] | None = Field(
        default=None,
        description="Tagi dodatkowe (poza automatycznym ``technology``).",
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
        description="``active`` / ``draft`` / ``deprecated``. Default ``active``.",
    )
    alternatives_rejected: list[_AlternativeArg] | None = Field(
        default=None,
        description=(
            "Alternatywne technologie odrzucone na rzecz tej — tabela "
            "``| Alternatywa | Dlaczego odrzucona |``."
        ),
    )
    links: list[_LinkArg] | None = Field(
        default=None,
        description="Linki zewnetrzne (docs / repo / wersja). Opcjonalnie.",
    )


class CreateTechnologyTool(Tool):
    """Tworzy notatke typu ``technology`` (konkretna biblioteka/serwis)."""

    name = "create_technology"
    description = (
        "Tworzy notatke typu 'technology' — konkretna technologia (np. 'Qdrant', "
        "'PostgreSQL'). Pole ``role`` jest wymagane i trafia do frontmattera. "
        "Strukturowane sekcje: role (prolog), 'Do czego uzywamy', opcjonalnie "
        "alternatywy odrzucone + linki. Finalizacja przez submit_plan."
    )

    def input_schema(self) -> dict[str, Any]:
        return _CreateTechnologyArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _CreateTechnologyArgs.model_validate(args)
        except ValidationError as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        normalized_path = normalize_path_or_error(parsed.path)
        if isinstance(normalized_path, ToolResult):
            return normalized_path

        if path_exists_effectively(ctx, normalized_path):
            return ToolResult(
                ok=False,
                error=(
                    f"path exists: {normalized_path!r} — technology o tej sciezce juz istnieje."
                ),
            )

        alts = None
        if parsed.alternatives_rejected:
            alts = [
                ConceptAlternative(name=a.name, reason=a.reason)
                for a in parsed.alternatives_rejected
            ]

        links = None
        if parsed.links:
            links = [TechnologyLink(label=l.label, url=l.url) for l in parsed.links]

        content = render_technology(
            title=parsed.title,
            role=parsed.role,
            used_for=parsed.used_for,
            parent=parsed.parent,
            related=parsed.related or None,
            tags=parsed.tags or None,
            created=parsed.created,
            updated=parsed.updated,
            status=parsed.status,
            alternatives_rejected=alts,
            links=links,
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
                    f"TECHNOLOGY created: title={parsed.title!r}, role={parsed.role!r}, "
                    f"parent={parsed.parent!r}."
                ),
            )
        return result
