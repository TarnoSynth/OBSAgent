"""``create_decision`` — tworzy notatke typu decision / ADR (Faza 5 refaktoru).

Decision to **ADR w stylu AthleteStack**. Oprocz samej notatki, narzedzie
**automatycznie dopisuje wiersz do tabeli 'Decyzje architektoniczne'**
w rodzicielskim hubie — jesli tabela istnieje. To idiom AthleteStack:
kazda decyzja = wpis w tabeli indeksujacej.

**Automatyzacja auto-append tabeli:**

Po utworzeniu notatki decision, narzedzie woluje (wewnetrznie) to samo co
``add_table_row(path=parent, heading='Decyzje architektoniczne', cells=...)``.
Gdy:

- parent nie jest hubem (np. MOC) — pomijamy bez bledu,
- hub istnieje ale nie ma tabeli 'Decyzje architektoniczne' — logujemy
  ``add_table_row skipped: no table`` w record_action, ale zwracamy ``ok=True``
  (notatka decision zostala zarejestrowana, hub moze byc dopiety rece usera),
- dopisanie sie udaje — rejestrujemy **dodatkowa akcje** w ``proposed_writes``
  (bez koalescencji z notatka decision, bo to inny plik).

**Pytanie otwarte #1 z planu:** "Czy ``create_decision`` ma automatycznie
wywolywac ``add_table_row`` do hubu?" → **TAK** (propozycja zaakceptowana).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext
from src.agent.tools.renderers.decision import DecisionConsequences, render_decision
from src.agent.tools.vault_write._common import (
    build_and_register_action,
    normalize_path_or_error,
    path_exists_effectively,
)
from src.agent.tools.vault_write._granular import (
    compute_effective_content,
    register_granular_update,
)
from src.agent.tools.vault_write._markdown_ops import (
    MarkdownOpsError,
    add_table_row,
    find_first_table_under_heading,
)

__all__ = ["CreateDecisionTool"]


DECISIONS_TABLE_HEADING = "Decyzje architektoniczne"


class _ConsequencesArg(BaseModel):
    """Struktura 'consequences' — pozytywne i negatywne skutki.

    Oba pola sa dozwolone puste (niektore decyzje maja czyste konsekwencje),
    ale typowy ADR ma conajmniej 1-2 wpisy w kazdej stronie.
    """

    model_config = ConfigDict(extra="forbid")

    positive: list[str] = Field(
        default_factory=list,
        description="Lista pozytywnych konsekwencji (po 1 zdaniu).",
    )
    negative: list[str] = Field(
        default_factory=list,
        description="Lista negatywnych konsekwencji / kosztow (po 1 zdaniu).",
    )


class _CreateDecisionArgs(BaseModel):
    """Schemat argumentow ``create_decision``."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ...,
        min_length=1,
        description=(
            "Sciezka relatywna ``.md`` (np. 'adr/UseQdrantOverPgvector.md'). "
            "Slug zwykle w CamelCase bez spacji."
        ),
    )
    title: str = Field(
        ...,
        min_length=1,
        description=(
            "Tytul ADR — trafi do ``# ADR — {title}``. Krotki, decyzja na stan "
            "imperatywny (np. 'Uzywamy Qdranta zamiast pgvector')."
        ),
    )
    summary: str = Field(
        ...,
        min_length=1,
        description=(
            "1-2 zdania prologu pod tytulem. Co zdecydowano i po co. Bez headingu."
        ),
    )
    context: str = Field(
        ...,
        min_length=1,
        description=(
            "Body sekcji '## Kontekst' — jaka sytuacja / ograniczenia / "
            "alternatywy rozwazane."
        ),
    )
    decision: str = Field(
        ...,
        min_length=1,
        description="Body sekcji '## Decyzja' — konkretne 'uzywamy X poniewaz ...'.",
    )
    rationale: str = Field(
        ...,
        min_length=1,
        description=(
            "Body sekcji '## Uzasadnienie' — rozszerzenie decyzji (dlaczego X, "
            "nie Y, nie Z)."
        ),
    )
    consequences: _ConsequencesArg = Field(
        ...,
        description="Struktura 'consequences' z listami 'positive' i 'negative'.",
    )
    parent: str = Field(
        ...,
        min_length=1,
        description=(
            "Wikilink do rodzica — zwykle hub albo MOC. Jesli parent to hub "
            "z tabela 'Decyzje architektoniczne', narzedzie AUTOMATYCZNIE "
            "dopisze do niej wiersz (krotki indeks decyzji)."
        ),
    )
    related: list[str] | None = Field(
        default=None,
        description="Lista wikilinkow do pola ``related`` frontmattera.",
    )
    tags: list[str] | None = Field(
        default=None,
        description="Tagi dodatkowe (``decision`` dodawany automatycznie).",
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
        description="``active`` / ``deprecated``. Default ``active``.",
    )
    migration: str | None = Field(
        default=None,
        description=(
            "Opcjonalny body sekcji '## Migracja' — co trzeba zrobic w kodzie/infra. "
            "``None`` albo pusty string → sekcja pominieta."
        ),
    )


class CreateDecisionTool(Tool):
    """Tworzy ADR-like notatke typu ``decision`` + auto-append do tabeli rodzica."""

    name = "create_decision"
    description = (
        "Tworzy notatke typu 'decision' (ADR w stylu AthleteStack) z "
        "strukturowanymi sekcjami: context, decision, rationale, consequences "
        "(positive/negative), opcjonalnie migration. Dodatkowo, jesli parent "
        "to hub z tabela 'Decyzje architektoniczne', automatycznie dopisuje "
        "wiersz do tej tabeli. Finalizacja przez submit_plan."
    )

    def input_schema(self) -> dict[str, Any]:
        return _CreateDecisionArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _CreateDecisionArgs.model_validate(args)
        except ValidationError as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        normalized_path = normalize_path_or_error(parsed.path)
        if isinstance(normalized_path, ToolResult):
            return normalized_path

        if path_exists_effectively(ctx, normalized_path):
            return ToolResult(
                ok=False,
                error=(
                    f"path exists: {normalized_path!r} — decision o tej sciezce juz istnieje."
                ),
            )

        consequences = DecisionConsequences(
            positive=list(parsed.consequences.positive),
            negative=list(parsed.consequences.negative),
        )

        content = render_decision(
            title=parsed.title,
            summary=parsed.summary,
            context=parsed.context,
            decision=parsed.decision,
            rationale=parsed.rationale,
            consequences=consequences,
            parent=parsed.parent,
            related=parsed.related or None,
            tags=parsed.tags or None,
            created=parsed.created,
            updated=parsed.updated,
            status=parsed.status,
            migration=parsed.migration,
        )

        create_result = build_and_register_action(
            ctx=ctx,
            tool_name=self.name,
            action_type="create",
            normalized_path=normalized_path,
            content=content,
        )
        if not create_result.ok:
            return create_result

        auto_table_note = self._try_add_to_parent_table(
            ctx=ctx,
            decision_path=normalized_path,
            decision_title=parsed.title,
            parent_wikilink=parsed.parent,
            summary_line=parsed.summary,
        )

        combined = (
            f"{create_result.content}\n"
            f"DECISION created: title={parsed.title!r}, parent={parsed.parent!r}.\n"
            f"{auto_table_note}"
        )
        return ToolResult(ok=True, content=combined)

    def _try_add_to_parent_table(
        self,
        *,
        ctx: ToolExecutionContext,
        decision_path: str,
        decision_title: str,
        parent_wikilink: str,
        summary_line: str,
    ) -> str:
        """Probuje dopisac wiersz do tabeli 'Decyzje architektoniczne' parenta.

        Zwraca krotki tekst opisujacy wynik (do ``ToolResult.content``):

        - ``"table updated: {parent}"``
        - ``"parent not found in vault: {parent}"``
        - ``"parent has no table 'Decyzje architektoniczne' — skip"``
        - ``"parent is not a hub (MOC etc.) — skip"``

        W kazdym przypadku **nie** zgrzyta ``ok=False`` — notatka decision
        juz jest zarejestrowana, a auto-indeks jest bonusem. Blad parsera
        markdowna logujemy przez ``ctx.record_action``.
        """

        parent_stem = _extract_wikilink_stem(parent_wikilink)
        if not parent_stem:
            return "auto-table: parent wikilink nierozpoznany — skip"

        if parent_stem.startswith("MOC___") or parent_stem.startswith("MOC__"):
            return "auto-table: parent to MOC (nie hub) — skip"

        parent_path = _resolve_parent_path(ctx, parent_stem)
        if parent_path is None:
            return f"auto-table: parent '{parent_stem}' nie znaleziony w vaulcie — skip"

        current = compute_effective_content(ctx, parent_path)
        if current is None:
            return f"auto-table: nie udalo sie odczytac parenta '{parent_path}' — skip"

        if find_first_table_under_heading(current, DECISIONS_TABLE_HEADING) is None:
            return (
                f"auto-table: parent '{parent_path}' nie ma tabeli "
                f"'{DECISIONS_TABLE_HEADING}' — skip"
            )

        decision_stem = Path(decision_path).stem
        cells = _infer_cells_from_table(
            content=current,
            decision_wikilink=f"[[{decision_stem}]]",
            decision_title=decision_title,
            summary_line=summary_line,
        )

        try:
            new_content = add_table_row(current, DECISIONS_TABLE_HEADING, cells)
        except MarkdownOpsError as exc:
            ctx.record_action(
                tool=self.name,
                path=parent_path,
                args={"auto_table": True, "heading": DECISIONS_TABLE_HEADING},
                ok=False,
                error=str(exc),
            )
            return f"auto-table: dopisanie nie powiodlo sie ({exc}) — skip"

        register_granular_update(
            ctx=ctx,
            tool_name=self.name,
            normalized_path=parent_path,
            new_content=new_content,
            op_summary=f"auto-append decision row to '{DECISIONS_TABLE_HEADING}'",
            extra_log_args={"decision_path": decision_path},
        )
        return f"auto-table: dopisano wiersz w '{parent_path}' → '{DECISIONS_TABLE_HEADING}'"


def _extract_wikilink_stem(wikilink: str) -> str | None:
    """Z ``"[[X]]"`` zwraca ``"X"``; z ``"[[X|alias]]"`` zwraca ``"X"``.

    Zwraca ``None`` jesli format nierozpoznany (np. puste, same spacje).
    """

    value = wikilink.strip()
    if not value:
        return None
    if value.startswith("[[") and value.endswith("]]"):
        inner = value[2:-2]
    else:
        inner = value
    stem = inner.split("|", 1)[0].strip()
    return stem or None


def _resolve_parent_path(ctx: ToolExecutionContext, stem: str) -> str | None:
    """Szuka parenta w vaulcie po stemie. Uwzglednia pending creates."""

    try:
        knowledge = ctx.ensure_vault_knowledge()
    except Exception:
        knowledge = None

    if knowledge is not None:
        resolved = knowledge.resolve(stem)
        if resolved is not None:
            return resolved.path

    for action in ctx.proposed_writes:
        if action.type == "create" and Path(action.path).stem == stem:
            return action.path

    return None


def _infer_cells_from_table(
    *,
    content: str,
    decision_wikilink: str,
    decision_title: str,
    summary_line: str,
) -> list[str]:
    """Heurystyka: odczytaj naglowek tabeli i dobierz wartosci komorek.

    Zakladamy, ze tabele decyzji u AthleteStack maja zwykle ``| Decyzja |
    Status | Opis |`` albo ``| Decyzja | Opis |``. Mapujemy:

    - kolumna zawierajaca "decyz" → ``[[wikilink]]`` (link do notatki)
    - kolumna "status" → ``active``
    - kolumna "opis" / "summary" / "streszczenie" → ``summary_line`` (flat)
    - pozostale → ``decision_title`` jako fallback (model moze poprawic).

    Gdy liczba kolumn > 3, reszta dostaje ``-`` — lepiej dopisac placeholdery
    niz zepsuc arity.
    """

    found = find_first_table_under_heading(content, DECISIONS_TABLE_HEADING)
    if found is None:
        return [decision_wikilink]
    _, table = found
    headers_lower = [h.lower() for h in table.headers]
    cells: list[str] = []
    one_liner = " ".join(summary_line.strip().splitlines())
    for header in headers_lower:
        if "decyz" in header or "adr" in header or header.strip() in ("", "name"):
            cells.append(decision_wikilink)
        elif "status" in header:
            cells.append("active")
        elif "opis" in header or "summary" in header or "streszcz" in header or "rationale" in header:
            cells.append(one_liner)
        elif "tytul" in header or "title" in header:
            cells.append(decision_title)
        else:
            cells.append("-")
    return cells
