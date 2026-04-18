"""Baza dla warstwy narzedzi agenta (Faza 0 refaktoru agentic tool loop).

**Rola w architekturze (docelowo):**

Warstwa ``src.agent.tools`` jest powiekszalnym rejestrem narzedzi, ktore
model LLM wola iteracyjnie w petli tool-use (pojawi sie w Fazie 1 refaktoru).
Kazde narzedzie to maly, wyspecjalizowany obiekt: ma ostry ``input_schema``,
jedno zadanie, jeden ``execute()``.

**Faza 0 zakres:** same fundamenty \u2014 protokol ``Tool``, typ wyniku
``ToolResult``, bez zadnej konkretnej implementacji. Pozwala to pozniejszym
fazom dorzucac narzedzia po jednym pliku, bez dotykania szkieletu.

**Kontrakt ``Tool``:**

- ``name`` \u2014 unikalna nazwa w snake_case (``create_hub``, ``add_table_row``).
  To nazwa widziana przez model i uzywana jako klucz w ``ToolRegistry``.
- ``description`` \u2014 jedno-dwu zdaniowy opis w 3. osobie (``"Tworzy nowa
  notatke typu hub..."``). Pojawia sie w prompcie modelu.
- ``input_schema()`` \u2014 zwraca JSON Schema argumentow wg konwencji
  OpenAI/Anthropic/OpenRouter tool calling. Schemat rowniez dokumentuje
  semantyke pol modelowi (przez ``description`` per property).
- ``execute(args, ctx)`` \u2014 asynchroniczne. Zwraca ``ToolResult``.
  **Nie wolno rzucac wyjatkow** \u2014 ``ToolRegistry.dispatch`` lapie tylko
  crashe nieprzewidziane (bug narzedzia), a domenowe bledy narzedzie
  sygnalizuje przez ``ToolResult(ok=False, error=...)`` \u2014 model dostaje
  ten blad w nastepnej turze i moze sie poprawic.

**Dlaczego ABC a nie Protocol:**

Protocol nie pozwala nadpisac atrybutow klasowych przez konkretne
subklasy w sposob, ktory mypy/Pyright rozumie bez hacks. ABC daje
jasny kontrakt "musisz zaimplementowac te metody" z kontrola tworzenia
instancji klasy abstrakcyjnej.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from src.agent.tools.context import ToolExecutionContext


class ToolResult(BaseModel):
    """Znormalizowany wynik pojedynczego ``tool.execute()``.

    **Dwie scierzki:**

    - **Sukces** (``ok=True``): ``content`` niesie tekstowy wynik, ktory
      trafia do modelu jako ``tool_result`` w kolejnej turze. ``structured``
      to opcjonalny slownik z surowymi danymi (np. lista notatek z
      ``list_notes``) \u2014 przydatny do logowania/debug, model widzi go
      zserializowanego w ``content``.
    - **Blad domenowy** (``ok=False``): ``error`` opisuje co poszlo nie
      tak (np. ``"path exists"``, ``"table 'Decyzje' not found"``).
      ``content`` zwykle pusty. Model ma szanse poprawic sie w kolejnej
      iteracji \u2014 dlatego domenowe bledy NIE sa rzucane jako wyjatki.

    **Kontrola:** ``content`` nie moze byc ``None``. Gdy nie ma co zwrocic
    tekstem, uzyj pustego stringa (np. na potwierdzenie ``"ok"``).

    Model ``frozen=False`` \u2014 narzedzia mozliwosc dopisac dane do
    ``structured`` po stworzeniu obiektu (choc zwykle tego nie potrzebuja).
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    content: str = ""
    structured: dict[str, Any] | None = None
    error: str | None = None

    def to_model_text(self) -> str:
        """Zwraca tekst, ktory trafi do modelu jako ``tool_result.content``.

        Gdy ``ok=True`` \u2014 wraca ``content``. Gdy ``ok=False`` \u2014 prefiks
        ``"ERROR: "`` + ``error`` + opcjonalny ``content`` jako kontekst.

        Model widzi wprost ``"ERROR: ..."`` i wie ze ma sie poprawic.
        """

        if self.ok:
            return self.content
        err = self.error or "unknown error"
        if self.content:
            return f"ERROR: {err}\n\n{self.content}"
        return f"ERROR: {err}"


class Tool(ABC):
    """Abstrakcyjna klasa bazowa dla jednego narzedzia agentowego.

    **Konwencje:**

    - ``name`` i ``description`` deklarowane jako atrybuty klasy (lub w
      ``__init__`` gdy wymagana parametryzacja). Sa widoczne dla modelu
      LLM przez ``ToolDefinition``.
    - ``input_schema()`` zwraca **pelny** JSON Schema \u2014 zwykle generowany
      z wewnetrznego modelu Pydantic (``ArgsModel.model_json_schema()``),
      z inliningiem ``$defs`` jesli provider tego wymaga. Patrz
      ``src.agent.models_actions._inline_definitions`` jako referencja.
    - ``execute(args, ctx)`` dostaje juz **zwalidowany** dict argumentow
      (poziom walidacji JSON: poprawny JSON, object). Dalsza walidacja
      (semantyka pol, typy Pydantic) nalezy do samego narzedzia \u2014
      najprosciej: ``args_model = ArgsModel.model_validate(args)``.

    **Cykl zycia narzedzia:**

    1. Zdefiniowane w swoim pliku w ``src/agent/tools/<category>/<name>.py``
    2. Rejestrowane w ``ToolRegistry.register(tool_instance)``
    3. W momencie builda requesta do modelu: ``registry.tool_definitions()``
       zwraca liste ``ToolDefinition`` dla providera.
    4. Gdy model wola narzedzie: ``registry.dispatch(tool_call, ctx)``
       parsuje argumenty i wola ``tool.execute(args, ctx)``.
    5. Wynik (``ToolResult``) trafia do logow + wraca do modelu jako
       ``tool_result`` w nastepnej turze.

    **Tools nie trzymaja stanu miedzy wywolaniami.** Caly stan biega w
    ``ToolExecutionContext`` \u2014 narzedzia sa pure-ish funkcjami nad
    contextem.
    """

    #: Unikalna nazwa narzedzia widoczna dla modelu LLM. Snake_case.
    name: str

    #: Opis w 3. osobie, jedno-dwu zdaniowy. Pojawia sie w prompcie modelu
    #: obok ``input_schema``. Powinien zaczynac sie czasownikiem.
    description: str

    @abstractmethod
    def input_schema(self) -> dict[str, Any]:
        """Zwraca JSON Schema parametrow dla providera LLM.

        Rekomendacja: miej wewnetrzny model Pydantic ``ArgsModel`` i zwroc
        ``ArgsModel.model_json_schema()`` z zainlinowanymi ``$defs`` \u2014
        patrz ``src.agent.models_actions._inline_definitions``.
        """

        raise NotImplementedError

    @abstractmethod
    async def execute(
        self,
        args: dict[str, Any],
        ctx: "ToolExecutionContext",
    ) -> ToolResult:
        """Wykonuje narzedzie na podanym contextie.

        ``args`` to juz sparsowany JSON (dict). Dalsza walidacja (schemat
        Pydantic, zakresy, istnienie sciezek) \u2014 po stronie narzedzia.

        **Nigdy nie rzucaj wyjatkow w przewidywanych sciezkach bledu.**
        Zwracaj ``ToolResult(ok=False, error="...")``. Wyjatki sa
        zarezerwowane na bugi implementacji (``ToolRegistry`` je zlapie
        i zaloguje jako ``tool.crashed``).
        """

        raise NotImplementedError


__all__ = ["Tool", "ToolResult"]
