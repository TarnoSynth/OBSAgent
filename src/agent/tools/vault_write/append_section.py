"""``append_section`` - dopisuje nowa sekcje na koncu notatki (Faza 3).

**Semantyka:**

Dodaje ``## {heading}\\n\\n{content}`` na koncu body istniejacej notatki.
NIE zapisuje na dysk - rejestruje ``ProposedWrite(type="update", ...)`` w
``ctx.proposed_writes`` z nowa pelna trescia pliku. Finalizacja leci przez
standardowy ``apply_pending`` flow.

**Preconditions:**

- ``path`` przechodzi walidacje (relatywny, ``.md``)
- plik istnieje (realnie lub jako pending create)
- sekcja o tym headingu jeszcze NIE istnieje (uzyj ``replace_section``)

**Koalescencja:**

Jesli wczesniej w tej sesji bylo ``create_note`` / ``update_note`` /
``append_section`` na ten sam plik, ``register_granular_update``
zastepuje ostatnia propozycje zamiast dodawac kolejna. Dzieki temu
preview w Obsidianie pokazuje jedna, spojna wersje docelowa.

**Poziom headingu:**

Domyslnie ``level=2`` (``##``). Dla sekcji zagniezdzonych (np.
``### Szczegoly implementacji`` pod ``## Modul``) model ustawia ``level=3``
jawnie. Zakres 1-6 (H1..H6).
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
    append_section,
)


class _AppendSectionArgs(BaseModel):
    """Schemat argumentow ``append_section``."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ...,
        min_length=1,
        description=(
            "Sciezka relatywna do istniejacej notatki .md, do ktorej dopisujemy sekcje. "
            "Plik musi juz istniec (albo byc wczesniej utworzony przez create_note)."
        ),
    )
    heading: str = Field(
        ...,
        min_length=1,
        description=(
            "Tytul nowej sekcji, bez znakow ``#``. Np. 'Historia zmian' albo "
            "'Decyzje architektoniczne'. Jesli sekcja o tym tytule juz istnieje "
            "w pliku — tool zwroci blad; uzyj wtedy 'replace_section'."
        ),
    )
    content: str = Field(
        ...,
        min_length=1,
        description=(
            "Tresc sekcji w markdownie — sam body bez linii headingu. Moga byc "
            "tabele, bullety, wikilinki, cokolwiek co renderuje Obsidian."
        ),
    )
    level: int = Field(
        2,
        ge=1,
        le=6,
        description=(
            "Poziom headingu (2 = ``##`` — default). Wyzsze liczby = glebiej "
            "zagniezdzona sekcja. Zakres 1-6 jak w markdownie."
        ),
    )


class AppendSectionTool(Tool):
    """Dopisuje nowa sekcje na koncu istniejacej notatki (proponuje - zapis po submit_plan)."""

    name = "append_section"
    description = (
        "Dodaje nowa sekcje (## heading + body) na koncu istniejacej notatki. "
        "Plik musi istniec w vaulcie. Jesli sekcja o takim tytule juz istnieje — "
        "zwroci blad; wtedy uzyj 'replace_section'. Jeden granulowany dopisek "
        "zamiast nadpisywania calej notatki. Nic nie zapisuje natychmiast - "
        "finalizacja przez submit_plan."
    )

    def input_schema(self) -> dict[str, Any]:
        return _AppendSectionArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _AppendSectionArgs.model_validate(args)
        except Exception as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        normalized = normalize_path_or_error(parsed.path)
        if isinstance(normalized, ToolResult):
            return normalized

        if not path_exists_effectively(ctx, normalized):
            return ToolResult(
                ok=False,
                error=(
                    f"path does not exist: {normalized!r} - uzyj 'create_note' "
                    f"zeby utworzyc nowa notatke zanim dopiszesz sekcje"
                ),
            )

        current = compute_effective_content(ctx, normalized)
        if current is None:
            return ToolResult(
                ok=False,
                error=(
                    f"Nie udalo sie odczytac biezacej tresci {normalized!r}. "
                    f"Sprawdz czy plik istnieje i jest czytelny."
                ),
            )

        try:
            new_content = append_section(
                current,
                heading=parsed.heading,
                section_body=parsed.content,
                level=parsed.level,
            )
        except MarkdownOpsError as exc:
            return map_markdown_error(self.name, exc, ctx, normalized)

        return register_granular_update(
            ctx=ctx,
            tool_name=self.name,
            normalized_path=normalized,
            new_content=new_content,
            op_summary=f"APPEND_SECTION '{parsed.heading}' (level={parsed.level})",
            extra_log_args={"heading": parsed.heading, "level": parsed.level},
        )


__all__ = ["AppendSectionTool"]
