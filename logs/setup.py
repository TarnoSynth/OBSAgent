"""Konfiguracja stdlib ``logging`` dla calej aplikacji.

Jeden punkt wejscia: ``configure_stdlib_logging(level, log_dir)`` —
- handler konsolowy na stderr (WARNING+)
- handler plikowy w ``log_dir/stdlib.log`` (poziom z parametru)
- format jednolinijkowy z timestampem, poziomem, nazwa loggera, message

Oddzielnie od RunLoggera: stdlib logging lapie logi z bibliotek (httpx,
anyio, asyncio) i wlasne ``logger.info()`` z ``src/*``. RunLogger zajmuje
sie tylko structured events agenta.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def configure_stdlib_logging(
    *,
    level: int | str = logging.INFO,
    log_dir: Path | None = None,
    console_level: int | str = logging.WARNING,
) -> None:
    """Konfiguruje globalny ``logging`` — konsola + plik (jesli ``log_dir``).

    Idempotentne: czyszczenie handlerow root loggera przed dodaniem nowych.
    """

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    console_handler = logging.StreamHandler(stream=sys.stderr)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(fmt)
    root.addHandler(console_handler)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            log_dir / "stdlib.log", encoding="utf-8"
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    # Wycisz noisy biblioteki na INFO — nie chcemy ich przy DEBUG=INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
