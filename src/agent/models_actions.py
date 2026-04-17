"""Modele `AgentAction` / `AgentResponse` + schema dla tool callingu.

Zgodnie z decyzja projektowa dla Fazy 6, AI zwraca plan dzialan
**wylacznie** przez tool calling \u2014 narzedzie `submit_plan` wymusza
schemat odpowiedzi po stronie providera. My dostajemy gotowy JSON
w `tool_calls[0].function.arguments`, parsujemy go przez Pydantic
i dostajemy twardy kontrakt.

Modele tej warstwy:

- ``AgentAction``   \u2014 jedna operacja na vaulcie (create / update / append)
- ``AgentResponse`` \u2014 lista akcji + summary (wszystko, co AI proponuje w biegu)

Walidacja Pydantic jest **pierwsza linia obrony**: scie\u017cki musz\u0105 byc
relatywne, bez ``..`` i bez wyjscia poza vault. Typy ograniczone do
literalnych wartosci. Dzieki temu preview pokazuje userowi tylko
propozycje, ktore przeszly schemat \u2014 bez halasu.

Druga linia obrony (`note_exists` vs typ akcji) siedzi w executorze
w trakcie wykonania, nie w samym modelu \u2014 Pydantic nie wie o stanie
vaulta.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


ActionType = Literal["create", "update", "append"]


class AgentAction(BaseModel):
    """Pojedyncza akcja proponowana przez AI do wykonania na vaulcie.

    Kontrakty:

    - ``type`` musi byc jednym z: ``create`` / ``update`` / ``append``
    - ``path`` jest **relatywny wzgledem vaulta**, zawsze ``.md``, bez
      ``..``, bez absolutnej sciezki, bez driveow
    - ``content``:
        - dla ``create`` i ``update`` \u2014 **pelna** tresc z frontmatterem
        - dla ``append`` \u2014 sam dopisek do body (bez frontmattera)

    Walidacja scieznowa jest **duplikowana** w ``VaultManager._resolve_safe_path``
    \u2014 tamta jest ostatnia twarda linia, ta tutaj odsiewa wiekszosc
    prob zanim dojda do executora.
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


class AgentResponse(BaseModel):
    """Pelna odpowiedz AI dla jednej iteracji (jeden commit projektowy).

    ``actions`` moze byc **pusta lista** \u2014 to sygnal od AI "ten commit
    nic semantycznie nie wnosi, nie ma co dokumentowac" (np. `bump deps`,
    `fix typo`). Agent wtedy pominie ten commit (ale go zaliczy do
    ``processed_commits`` \u2014 decyzja po stronie agenta, nie AI).
    """

    summary: str = Field(..., description="1-2 zdania: co zrobil AI w tej iteracji i dlaczego.")
    actions: list[AgentAction] = Field(default_factory=list)

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str) -> str:
        if not value or not isinstance(value, str):
            raise ValueError("summary musi byc niepustym stringiem")
        stripped = value.strip()
        if not stripped:
            raise ValueError("summary nie moze byc tylko bialymi znakami")
        return stripped


SUBMIT_PLAN_TOOL_NAME = "submit_plan"

SUBMIT_PLAN_TOOL_DESCRIPTION = (
    "Zwroc plan dzialan na vaulcie dla analizowanego commita projektowego. "
    "Wywolaj to narzedzie DOKLADNIE RAZ. Pusta lista `actions` jest dozwolona "
    "\u2014 wtedy w `summary` wyjasnij dlaczego commit nie wymaga dokumentacji."
)


def build_submit_plan_schema() -> dict[str, Any]:
    """Buduje JSON schema parametrow narzedzia `submit_plan`.

    Generowany z modelu Pydantic ``AgentResponse`` \u2014 jedno zrodlo prawdy.
    Wynik jest kompatybilny z OpenAI/OpenRouter tools i Anthropic
    input_schema (oba akceptuja ten sam kompaktowy JSON Schema).

    Gdy schemat Pydantica bedzie sie zmienial, ten generator automatycznie
    za nim podaza \u2014 nie ma podwojnego utrzymania.
    """

    schema = AgentResponse.model_json_schema()

    return _inline_definitions(schema)


def _inline_definitions(schema: dict[str, Any]) -> dict[str, Any]:
    """Rozwija ``$defs`` z top-levelu schematu do inline subschematow.

    Providerzy (szczegolnie Anthropic) nie zawsze radza sobie z ``$ref``
    i ``$defs`` \u2014 bezpieczniej spelnic caly schemat od reki w jednym
    miejscu. Funkcja jest minimalna: zaklada, ze ``$defs`` i odwolania
    ``$ref`` wystepuja tylko raz i niezagnieezdzone (co jest prawda dla
    ``AgentResponse``).
    """

    defs = schema.pop("$defs", None) or schema.pop("definitions", None) or {}

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node and isinstance(node["$ref"], str):
                ref = node["$ref"]
                if ref.startswith("#/$defs/") or ref.startswith("#/definitions/"):
                    key = ref.split("/")[-1]
                    if key in defs:
                        return _resolve(defs[key])
            return {k: _resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_resolve(item) for item in node]
        return node

    return _resolve(schema)
