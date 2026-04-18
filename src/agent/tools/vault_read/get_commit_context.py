"""``get_commit_context`` — metadane biezacego commita (Faza 4 refaktoru).

**Semantyka:**

Zwraca strukturowany opis commita, dla ktorego agent prowadzi sesje
tool-use: SHA, message, author, date, stats, lista zmienionych plikow
(ze skrocona informacja o zmianie, bez pelnych diffow).

**Kiedy uzywac:**

Prompt builder w Fazach 0-3 wrzucal commit do user-prompta jako tekst.
Faza 4 zostawia to zachowanie (kompatybilnosc), ale rowniez daje modelowi
**jawne narzedzie**, gdyby potrzebowal przypomniec sobie dane commita w
srodku dluzszej pętli (np. po 5 wywolaniach ``list_notes``, zanim napisze
changelog).

**Dlaczego nie zwracamy pelnego diffa:**

Pelne diffy sa juz w user-prompcie (albo w summaries chunkow dla trybu
multi-turn). Duplikacja zjadalaby tokeny. ``get_commit_context`` daje
skondensowana mape "co sie zmienilo, jaki rozmiar, jakie pliki". Model,
ktory chce konkretny diff, patrzy w historie konwersacji, nie tu.

**Bezpieczenstwo:**

Gdy ``ctx.commit_info`` jest ``None`` (np. narzedzie wywolane z external
MCP inspector bez aktywnej sesji commitowej) — zwracamy ``ok=False`` z
czytelnym komunikatem. Nie ma mockowanej wartosci.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext


class _GetCommitContextArgs(BaseModel):
    """``get_commit_context`` nie przyjmuje zadnych argumentow."""

    model_config = ConfigDict(extra="forbid")


class GetCommitContextTool(Tool):
    """Zwraca metadane biezacego commita projektowego (SHA, message, pliki)."""

    name = "get_commit_context"
    description = (
        "Zwraca metadane commita projektowego, dla ktorego trwa sesja tool-use: "
        "SHA, message, author, date, stats (+/-), lista zmienionych plikow "
        "(path + change_type, bez pelnych diffow). Uzyj, gdy w dluzszej petli "
        "potrzebujesz przypomniec sobie podstawowe info o commicie."
    )

    def input_schema(self) -> dict[str, Any]:
        return _GetCommitContextArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            _GetCommitContextArgs.model_validate(args)
        except Exception as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        if ctx.commit_info is None:
            return ToolResult(
                ok=False,
                error=(
                    "get_commit_context: brak aktywnego commita w kontekscie. "
                    "Narzedzie dostepne tylko w trakcie sesji agenta, nie z external MCP client."
                ),
            )

        commit = ctx.commit_info

        changes: list[dict[str, Any]] = []
        for change in commit.changes:
            entry: dict[str, Any] = {
                "path": change.path,
                "change_type": str(change.change_type),
            }
            if change.old_path:
                entry["old_path"] = change.old_path
            changes.append(entry)

        structured: dict[str, Any] = {
            "sha": commit.sha,
            "sha_short": commit.sha[:7],
            "message": commit.message,
            "author": commit.author,
            "date": commit.date.isoformat(),
            "stats": {
                "insertions": commit.stats.insertions,
                "deletions": commit.stats.deletions,
            },
            "changes_count": len(changes),
            "changes": changes,
        }

        ctx.record_action(
            tool=self.name,
            path=None,
            args={"sha": commit.sha, "changes_count": len(changes)},
            ok=True,
        )

        header = (
            f"commit {commit.sha[:7]} by {commit.author} "
            f"(+{commit.stats.insertions}/-{commit.stats.deletions}, {len(changes)} plikow)"
        )
        body = json.dumps(structured, ensure_ascii=False, indent=2)
        return ToolResult(
            ok=True,
            content=f"{header}\n{body}",
            structured=structured,
        )


__all__ = ["GetCommitContextTool"]
