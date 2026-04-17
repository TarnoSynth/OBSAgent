"""Kontrakt sinku: zjada ``Event`` i gdzies go zapisuje.

Sink MUSI byc tolerancyjny na bledy — nigdy nie podnosi wyjatku w gore.
Logging nigdy nie moze wywalic main flow agenta. Bledy wewnetrzne sinku
leca do stderr przez ``logging.getLogger("obsagent.logs.<nazwa>").error``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from logs.events import Event


@runtime_checkable
class Sink(Protocol):
    """Protokol sinku eventow."""

    name: str

    def emit(self, event: Event) -> None:
        """Zapisuje event. NIE moze podniesc wyjatku."""
        ...

    def close(self) -> None:
        """Flush + zwolnij zasoby (plik, http client). Idempotentne."""
        ...
