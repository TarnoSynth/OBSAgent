"""Wspólny kontrakt providerów LLM: typy danych (Pydantic) i abstrakcyjna baza BaseProvider.

Aplikacja operuje na ChatRequest / ProviderResult; konkretne klasy (OpenAI, Anthropic, …)
mapują to na swoje SDK — bez wyciekania szczegółów API do reszty kodu.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any, Sequence

from pydantic import BaseModel, Field


class MessageRole(StrEnum):
    """Role wiadomości w konwersacji (wartości zgodne z typowymi API chat).

    TOOL: wynik narzędzia; konkretny provider może wymagać dodatkowych pól (np. id wywołania).
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ChatMessage(BaseModel):
    """Pojedyncza wiadomość w formacie niezależnym od dostawcy.

    name: opcjonalnie przy USER (np. rozróżnienie użytkowników w jednym wątku).
    tool_call_id: powiązanie odpowiedzi narzędzia z wcześniejszym wywołaniem assistant.tool_calls.
    tool_calls: lista wywołań narzędzi zwrócona przez model w wiadomości assistant.
    """

    role: MessageRole
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list["ToolCall"] = Field(default_factory=list)


class ToolFunctionDefinition(BaseModel):
    """Definicja funkcji udostępnianej modelowi przez tool calling."""

    name: str
    description: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolDefinition(BaseModel):
    """Jedno narzędzie w formacie zbliżonym do OpenAI/OpenRouter Chat API."""

    type: str = "function"
    function: ToolFunctionDefinition


class ToolFunctionCall(BaseModel):
    """Wywołanie konkretnej funkcji przez model."""

    name: str
    arguments: str


class ToolCall(BaseModel):
    """Jedno wywołanie narzędzia zwrócone przez model lub odsyłane z powrotem do API."""

    id: str | None = None
    type: str = "function"
    function: ToolFunctionCall


class UsageStats(BaseModel):
    """Zużycie tokenów po stronie API (jeśli zwrócone).

    ``cache_creation_input_tokens`` i ``cache_read_input_tokens`` dotycza
    prompt cachingu Anthropica: ``creation`` to tokeny zapisane do cache
    w tym wywolaniu (koszt 1.25x), ``read`` to tokeny odczytane z cache
    (koszt 0.1x). Dla OpenAI/OpenRouter pozostaja ``None``.

    Wazne: Anthropic raportuje ``input_tokens`` jako liczbe tokenow, ktore
    NIE byly ani cache-read ani cache-create. Pelna liczba tokenow
    wejsciowych to suma wszystkich trzech pol.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


class ChatRequest(BaseModel):
    """Parametry jednego wywołania modelu — wspólne dla wszystkich providerów.

    Pola opcjonalne (None): nie są przekazywane dalej, chyba że provider ma sensowny default.
    messages: jedyne pole wymagane przy typowym użyciu.

    extra: słownik na rzadkie lub specyficzne dla jednego backendu klucze; merge zwykle na końcu
    budowania kwargs (uwaga na nadpisania).
    """

    messages: Sequence[ChatMessage]
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    stop: str | Sequence[str] | None = None
    top_p: float | None = None
    tools: list[ToolDefinition] = Field(default_factory=list)
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ProviderResult(BaseModel):
    """Znormalizowana odpowiedź dla aplikacji (niezależnie od kształtu JSON z HTTP).

    text: główna treść asystenta.
    model: faktycznie użyty model (może różnić się od żądanego przy fallbackach).
    finish_reason: jak zakończyła się generacja (np. stop, length) — string z API lub None.
    usage: tokeny jeśli dostępne.
    raw: opcjonalnie pełny obiekt odpowiedzi z SDK / surowy payload do debugowania.
    """

    text: str
    model: str
    finish_reason: str | None = None
    usage: UsageStats | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    raw: Any = None


class BaseProvider(ABC):
    """Abstrakcyjna baza: jedna metoda complete(request) dla całej aplikacji.

    name: identyfikator backendu (np. 'openai') — logi, factory, metryki.
    default_model: wartość z konfiguracji, gdy ChatRequest.model jest None.

    Podklasy implementują complete; mapowanie ChatRequest → wywołanie biblioteki jest po ich stronie.
    """

    def __init__(self, *, name: str, default_model: str) -> None:
        self.name = name
        self.default_model = default_model

    def _resolve_model(self, request: ChatRequest) -> str:
        """Zwraca model z requestu albo default_model z konstruktora."""
        return request.model or self.default_model

    @abstractmethod
    async def complete(self, request: ChatRequest) -> ProviderResult:
        """Wykonuje zapytanie do API i zwraca ProviderResult.

        Implementacja w klasach OpenAIProvider, AnthropicProvider itd.
        """
        raise NotImplementedError
