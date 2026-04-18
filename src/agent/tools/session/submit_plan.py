"""``submit_plan`` - terminator sesji tool-use (Faza 2 refaktoru agentic tool loop).

**Rola:**

Do Fazy 1 ``submit_plan`` bylo jednorazowym toolem z pelnym schematem
``ProposedPlan`` (summary + writes). W Fazie 2 pisy sa rejestrowane
rozproszenie przez ``create_note`` / ``update_note`` / ``append_to_note``,
a ``submit_plan`` staje sie prostym **sygnalem zakonczenia sesji** z
jednym polem: ``summary``.

**Semantyka:**

- Model woła ``submit_plan(summary="...")`` gdy uzna, ze wszystkie tool
  cally zwiazane z tym commitem juz zostaly wykonane.
- Tool ustawia ``ctx.finalized = True`` i ``ctx.final_summary = summary``.
- Agent sprawdza ``ctx.finalized`` po kazdej turze pętli - ``True`` =
  wyjscie, konwersja ``ctx.proposed_writes`` na ``ProposedPlan``.
- Jeden call wystarcza; drugi call nadpisuje summary, ale flaga juz
  ``True``, wiec agent i tak wyjdzie po pierwszej iteracji w ktorej byl.

**Nie rejestruje zadnej akcji write** - to czysto sterujacy tool. Na odwrot
od wersji Fazy 1, nie niesie listy ``actions`` w argumentach.

**Dlaczego ``summary`` jest wymagane:**

Summary trafia do ``ProposedPlan.summary`` i pokazuje sie userowi w preview
+ jest commit message dla vault (``commit_vault``). Bez tego nie da sie
zbudowac sensownego ``ProposedPlan`` — Pydantic walidator summary wymaga
niepustego stringa.

Gdy model woła ``submit_plan`` z pustym/bialym summary, zwracamy
``ToolResult(ok=False, error=...)`` zeby model mial szanse sie poprawic.
Nie finalizujemy sesji - model musi sprobowac jeszcze raz z poprawnym summary.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext


class _SubmitPlanArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        ...,
        min_length=1,
        description=(
            "1-2 zdania: co zrobiles w tej sesji i dlaczego. Trafi do preview dla "
            "usera + zostanie uzyte jako commit message w vaulcie. Powinno opisywac "
            "sens dokumentowanego commita projektu w jezyku domenowym, nie implementacyjnie."
        ),
    )


class SubmitPlanTool(Tool):
    """Sygnal zakonczenia sesji tool-use. Ustawia ``ctx.finalized = True``."""

    name = "submit_plan"
    description = (
        "Zakoncz sesje tool-use. Wywolaj to narzedzie DOKLADNIE RAZ, kiedy "
        "uznasz ze wszystkie create_note/update_note/append_to_note juz zostaly "
        "zarejestrowane dla tego commita projektu. 'summary' powinno opisywac 1-2 "
        "zdaniami sens wprowadzonych zmian dokumentacyjnych. Jesli commit nie wymaga "
        "zadnych zmian w vaulcie, i tak wywolaj submit_plan z summary wyjasniajacym dlaczego."
    )

    def input_schema(self) -> dict[str, Any]:
        return _SubmitPlanArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        summary_raw = args.get("summary")
        if not isinstance(summary_raw, str):
            return ToolResult(
                ok=False,
                error="argument 'summary' jest wymagany i musi byc stringiem",
            )
        summary = summary_raw.strip()
        if not summary:
            return ToolResult(
                ok=False,
                error="'summary' nie moze byc pusty lub samymi bialymi znakami",
            )

        ctx.finalize(summary)
        ctx.record_action(
            tool=self.name,
            path=None,
            args={"summary_len": len(summary)},
            ok=True,
        )

        proposed_count = len(ctx.proposed_writes)
        return ToolResult(
            ok=True,
            content=(
                f"Sesja zakonczona. Zarejestrowano {proposed_count} propozycji write "
                f"do akceptacji przez usera."
            ),
        )


__all__ = ["SubmitPlanTool"]
