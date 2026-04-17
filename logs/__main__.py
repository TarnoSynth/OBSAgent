"""Entrypoint dla ``python -m logs <cmd>``.

Deleguje do ``logs.cli.main``. Istnieje jako osobny plik, zeby Python
traktowal pakiet jako uruchamialny i zeby ``cli.py`` mogl byc importowany
bez automatycznego parsowania argv.
"""

from __future__ import annotations

import sys

from logs.cli import main

if __name__ == "__main__":
    sys.exit(main())
