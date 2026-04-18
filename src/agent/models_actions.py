"""Modele ``ProposedWrite`` / ``ProposedPlan`` / ``SessionResult`` (Faza 7).

**Historycznie (Fazy 0-6):** AI zwracalo pojedynczy ``submit_plan(actions=[...])``
z calym planem w jednym JSON-ie. Modele nazywaly sie ``AgentAction`` i
``AgentResponse``, a generator ``build_submit_plan_schema`` eksponowal ten
schemat jako ``tools=[submit_plan]`` w ``ChatRequest``.

**Po Fazie 7:** AI dziala w pelnej petli tool-use — rejestruje operacje
rozproszonymi tool callami (``create_note`` / ``update_note`` / ``append_section``
/ ...), a ``submit_plan`` jest wylacznie sygnalem zakonczenia sesji
(argument: ``summary``). Struktury zostaly przemianowane, zeby nazwy
odzwierciedlaly nowa semantyke:

- ``ProposedWrite``  — jedna operacja zapisu zaproponowana przez tool call
  (dawniej ``AgentAction``). Zyje w ``ToolExecutionContext.proposed_writes``.
- ``ProposedPlan``   — suma propozycji z calej sesji + ``summary`` (dawniej
  ``AgentResponse``). Budowany przez agenta po wyjsciu z petli, konsumowany
  przez ``apply_pending`` / ``finalize_pending`` / preview.
- ``SessionResult``  — ``ProposedPlan`` + metryki sesji (iterations_used,
  tool_calls_count, finalized_by_submit_plan).

Walidacja Pydantic dla ``ProposedWrite.path`` jest **pierwsza linia obrony**
— zduplikowana w ``VaultManager._resolve_safe_path`` dla pewnosci. Walidator
content dopuszcza pusty string tylko dla append; dla create/update wymaga
niepustej tresci (bo to cala notatka z frontmatterem).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


ActionType = Literal["create", "update", "append"]


class ProposedWrite(BaseModel):
    """Pojedyncza propozycja zapisu zbierana w trakcie sesji tool-use.

    Reprezentuje efekt pojedynczego tool calla (np. ``create_note``,
    ``update_note``, ``append_section``) jako atomowy patch na vaulcie.
    Zbierane w ``ToolExecutionContext.proposed_writes`` przez warstwe
    ``src/agent/tools/vault_write/_common.py`` — zadne narzedzie nie pisze
    do dysku przed ``apply_pending``.

    Kontrakty pol:

    - ``type``: ``create`` / ``update`` / ``append``
    - ``path``: **relatywny** wzgledem vaulta, zawsze ``.md``, bez ``..``,
      bez drive-letter (Windows), bez wiodacego ``/``
    - ``content``:
        - dla ``create`` i ``update`` — **cala** tresc pliku (wraz z
          frontmatterem YAML)
        - dla ``append`` — sam dopisek do body (bez frontmattera)
    """

    type: ActionType
    path: str = Field(..., description="Sciezka relatywna w vaulcie, konczaca sie .md")
    content: str = Field(..., description="Tresc notatki (cala dla create/update, dopisek dla append)")

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value or not isinstance(value, str):
            raise ValueError("path musi byc niepustym stringiem")

        stripped = value.strip()
        if not stripped:
            raise ValueError("path nie moze byc bialymi znakami")
        if stripped != value:
            raise ValueError(f"path zawiera niepotrzebne biale znaki na koncu/poczatku: {value!r}")

        normalized = stripped.replace("\\", "/")
        if normalized.startswith("/"):
            raise ValueError(f"path musi byc relatywny, dostalismy {value!r}")
        if ":" in normalized.split("/", 1)[0]:
            raise ValueError(f"path nie moze zawierac drive-letter (np. C:/): {value!r}")
        if any(part == ".." for part in normalized.split("/")):
            raise ValueError(f"path nie moze zawierac '..': {value!r}")
        if not normalized.lower().endswith(".md"):
            raise ValueError(f"path musi konczyc sie na '.md', dostalismy {value!r}")
        if normalized.endswith("/") or "//" in normalized:
            raise ValueError(f"path ma nieprawidlowa strukture: {value!r}")

        return normalized

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        if value is None or not isinstance(value, str):
            raise ValueError("content musi byc stringiem")
        if value == "":
            raise ValueError("content nie moze byc pusty")
        return value


class ProposedPlan(BaseModel):
    """Zbiorczy plan zapisow + ``summary`` po zakonczonej sesji tool-use.

    Budowany przez ``Agent._build_proposed_plan`` z ``ctx.proposed_writes``
    + ``ctx.final_summary`` po wywolaniu ``submit_plan``. Konsumowany przez:

    - ``plan_post_updates`` — pre-compute planow MOC/_index (safety net po Fazie 7).
    - ``apply_pending``     — zapisuje diff-view do vaulta.
    - ``PreviewRenderer``   — rich tabela w terminalu.

    ``writes`` moze byc pusta lista — commit projektowy ktory nic nie wnosi
    semantycznie (np. bump deps, fix typo). Agent i tak zaliczy go do
    ``processed_commits`` z odpowiednia adnotacja w preview.
    """

    summary: str = Field(..., description="1-2 zdania: co zrobil AI w tej sesji i dlaczego.")
    writes: list[ProposedWrite] = Field(default_factory=list)

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str) -> str:
        if not value or not isinstance(value, str):
            raise ValueError("summary musi byc niepustym stringiem")
        stripped = value.strip()
        if not stripped:
            raise ValueError("summary nie moze byc tylko bialymi znakami")
        return stripped


class SessionResult(BaseModel):
    """Wynik pojedynczej sesji ``Agent.run_session`` (Faza 2 refaktoru, Faza 7 rename).

    **Semantyka:**

    Sesja = jeden commit projektowy przechodzi przez petle tool-use
    (``create_note`` / ``update_note`` / ``append_to_note`` / ... + narzedzia
    eksploracyjne + domain creators + ``submit_plan`` terminator). Po wyjsciu
    z petli agent zwraca ``SessionResult``, ktory niesie *wszystkie*
    informacje potrzebne na zewnatrz:

    - ``plan`` — ``ProposedPlan`` (summary + writes) zbudowany
      z ``ctx.final_summary`` + ``ctx.proposed_writes``. Konsumowany przez
      ``apply_pending`` / ``finalize_pending`` / preview.
    - ``iterations_used`` — ile iteracji petli wykonal model (dla logow
      i heurystyk "czy trzeba podnosic max_tool_iterations").
    - ``tool_calls_count`` — ile tool callow model wywolal lacznie (liczy
      sie KAZDE wywolanie, rowniez te ktore padly walidacja).
    - ``finalized_by_submit_plan`` — ``True`` gdy model zakonczyl przez
      ``submit_plan`` (oczekiwana sciezka). Po Fazie 7 zawsze ``True`` — petla
      bez submit_plan rzuca ``_SessionValidationError`` i retry; fallback
      exit z pustym summary zostal usuniety.
    """

    model_config = ConfigDict(extra="forbid")

    plan: ProposedPlan
    iterations_used: int = Field(..., ge=0)
    tool_calls_count: int = Field(..., ge=0)
    finalized_by_submit_plan: bool

    @property
    def summary(self) -> str:
        """Skrot do ``plan.summary`` — czesty helper w main.py / preview."""

        return self.plan.summary

    @property
    def writes(self) -> list[ProposedWrite]:
        """Skrot do ``plan.writes`` — czesty helper w apply_pending i testach."""

        return self.plan.writes


__all__ = [
    "ActionType",
    "ProposedPlan",
    "ProposedWrite",
    "SessionResult",
]
