"""``add_related_link`` - dopisuje wikilink do ``related[]`` we frontmatterze (Faza 3).

**Semantyka:**

Parsuje YAML frontmatter, dopisuje wikilink do pola ``related`` (jesli
nie ma — tworzy nowa liste). Idempotentne: drugi call z tym samym
``wikilink`` nie duplikuje.

**Kiedy uzywac:**

- Po zauwazeniu powiazania miedzy modulem A i B → ``add_related_link("A.md", "[[B]]")``
  + ``add_related_link("B.md", "[[A]]")`` (ma sens wzajemnie).
- Po utworzeniu ADR wplywajacego na istniejacy modul → link w obu strony.

**Dlaczego osobne narzedzie a nie ``update_frontmatter``:**

``update_frontmatter(field="related", value=["[[A]]", "[[B]]"])`` **zastapi**
cala liste. Jesli wczesniej byly tam ``[[C]]`` i ``[[D]]`` — znikaja. Model
musialby czytac stary stan, dokladac, zapisywac. Zmudnie i ryzykownie.

Osobne ``add_related_link`` robi to idempotentnie: czyta, sprawdza, dopisuje
(albo nie, gdy juz jest). Dwie linijki schema, zerowe ryzyko zjedzenia danych.

**Preconditions:**

- ``path`` przechodzi walidacje (relatywny, ``.md``)
- plik istnieje (realnie lub jako pending create)
- pole ``related`` w frontmatterze jest lista (albo nie istnieje)
- wikilink niepusty
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
    add_to_frontmatter_list,
)


class _AddRelatedLinkArgs(BaseModel):
    """Schemat argumentow ``add_related_link``."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ...,
        min_length=1,
        description="Sciezka relatywna do istniejacej notatki .md.",
    )
    wikilink: str = Field(
        ...,
        min_length=1,
        description=(
            "Wikilink do dopisania, np. '[[Auth]]' albo 'Auth' (auto-owiniemy w "
            "``[[...]]``). Idempotentne — jesli juz jest w related, no-op."
        ),
    )


class AddRelatedLinkTool(Tool):
    """Dopisuje wikilink do related[] we frontmatterze (idempotentnie)."""

    name = "add_related_link"
    description = (
        "Dopisuje wikilink do listy 'related' w YAML frontmatterze. Jesli pole "
        "nie istnieje — tworzy. Idempotentne — nie duplikuje istniejacych wpisow. "
        "Uzywaj do powiazan miedzy notatkami zamiast update_frontmatter (ktore "
        "zastapiloby cala liste). Nic nie zapisuje natychmiast - finalizacja "
        "przez submit_plan."
    )

    def input_schema(self) -> dict[str, Any]:
        return _AddRelatedLinkArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _AddRelatedLinkArgs.model_validate(args)
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

        wikilink = parsed.wikilink.strip()
        if not (wikilink.startswith("[[") and wikilink.endswith("]]")):
            wikilink = f"[[{wikilink}]]"

        try:
            new_content, added = add_to_frontmatter_list(
                current, field="related", value=wikilink, deduplicate=True,
            )
        except MarkdownOpsError as exc:
            return map_markdown_error(self.name, exc, ctx, normalized)

        if not added:
            ctx.record_action(
                tool=self.name,
                path=normalized,
                args={"wikilink": wikilink, "result": "noop_already_present"},
                ok=True,
            )
            return ToolResult(
                ok=True,
                content=(
                    f"Wikilink {wikilink} juz byl w related[] pliku {normalized!r} "
                    f"- no-op (idempotencja)."
                ),
            )

        return register_granular_update(
            ctx=ctx,
            tool_name=self.name,
            normalized_path=normalized,
            new_content=new_content,
            op_summary=f"ADD_RELATED_LINK {wikilink}",
            extra_log_args={"wikilink": wikilink},
        )


__all__ = ["AddRelatedLinkTool"]
