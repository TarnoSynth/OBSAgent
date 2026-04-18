"""``VaultOperations`` - atomowe operacje zapisu na vaulcie (Faza 2 refaktoru agentic tool loop).

**Rola:**

Warstwa posredniczaca miedzy narzedziami ``Tool`` (``create_note``, ``update_note``,
``append_to_note``, a w Fazie 3+ granulowanymi: ``add_table_row``, ``append_section``...)
a ``VaultManager``. Wyciaga cztery rzeczy, ktore do Fazy 1 zyly w ``ActionExecutor``:

1. **Walidacja sciezki** — relatywna, bez ``..``, konczy sie ``.md``, bez drive-letter
   (dokladnie ta sama, co w ``ProposedWrite._validate_path``).
2. **Walidacja preconditions** — ``create`` tylko na nieistniejacej sciezce,
   ``update`` / ``append`` tylko na istniejacej.
3. **Cienkie wrappery** nad ``VaultManager.create/overwrite/append`` zwracajace
   znormalizowany ``OperationReport`` (``ok``, ``path``, ``error``).
4. **Brak pending wrappingu** — `VaultOperations` pisze **raw**. Logike diff-view
   (red + green) dalej trzyma ``ActionExecutor.apply_pending`` (Fazy 2-3).

**Czego NIE robi:**

- Nie snapshotuje stanu przed zapisem (to ``pending.capture_snapshot``).
- Nie rejestruje wykonanej akcji w ``ToolExecutionContext.executed_actions``
  — to zadanie narzedzia (Tool.execute), bo to ono ma pelny kontekst args.
- Nie mysli o rollbacku — rollback zyje w ``pending``.

**Dlaczego osobna klasa, a nie metody bezposrednio w ``VaultManager``:**

``VaultManager`` nic nie wie o semantyce agenta (create vs overwrite vs append
jako rodzina ``ProposedWrite.type``). On wystawia primitywne ``create/overwrite/append``.
Warstwa ``VaultOperations`` siedzi **jeden poziom wyzej**, mowi "jestem agent
chcacy zrobic operacje typu create — sprawdz preconditions, walicuj path, odbij
blad semantyczny jako OperationReport". Dzieki temu ``Tool.execute`` sie nie
duplikuje.

**Kontrakt wyjsciowy ``OperationReport``:**

- ``ok=True``  → zapis sie udal, ``path`` to znormalizowana sciezka (slash-forward).
- ``ok=False`` → zapis sie nie udal, ``error`` niesie czytelny komunikat. Operacja
  zostawila vault w stanie sprzed wywolania (walidacja jest zawsze **przed** I/O
  — jesli ``create`` odrzuca ``path exists``, to nic nie zostalo zapisane).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.vault.manager import VaultManager

logger = logging.getLogger(__name__)


OperationType = Literal["create", "update", "append"]


class InvalidPathError(ValueError):
    """Sciezka nie przeszla walidacji (bez ``..``, relatywna, konczy sie ``.md``).

    Wyciagniete do osobnego typu wyjatku, zeby narzedzia mogly rozroznic
    ``ValueError`` z Pydantic (inne pole) od konkretnego bledu sciezki.
    """


class OperationReport(BaseModel):
    """Znormalizowany wynik pojedynczego ``VaultOperations.*`` wywolania.

    Uzywany jako wewnetrzny typ komunikacji miedzy ``Tool.execute`` a
    ``VaultOperations``. Narzedzia mapuja ``OperationReport`` na ``ToolResult``:

    - ``ok=True``  → ``ToolResult(ok=True, content=f"created {path}")``
    - ``ok=False`` → ``ToolResult(ok=False, error=error)``
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    op: OperationType
    path: str = Field(..., description="Znormalizowana sciezka relatywna (forward-slash)")
    error: str | None = None


def validate_relative_md_path(value: str) -> str:
    """Waliduje i normalizuje sciezke relatywna do vaulta.

    Reguly (identyczne z ``ProposedWrite._validate_path``):

    - niepusty string
    - bez bialych znakow na brzegach
    - zamiana ``\\`` na ``/``
    - nie zaczyna sie ``/`` (nie absolutna)
    - brak drive-letter (``C:``) w pierwszym segmencie
    - zadne segmenty nie sa ``..``
    - konczy sie ``.md`` (case-insensitive)
    - bez podwojnych ``//`` i bez ``/`` na koncu

    :returns: znormalizowana sciezka (forward-slash).
    :raises InvalidPathError: gdy sciezka nie przechodzi walidacji.
    """

    if not value or not isinstance(value, str):
        raise InvalidPathError("path musi byc niepustym stringiem")

    stripped = value.strip()
    if not stripped:
        raise InvalidPathError("path nie moze byc samymi bialymi znakami")
    if stripped != value:
        raise InvalidPathError(
            f"path zawiera niepotrzebne biale znaki na koncu/poczatku: {value!r}"
        )

    normalized = stripped.replace("\\", "/")
    if normalized.startswith("/"):
        raise InvalidPathError(f"path musi byc relatywny, dostalismy {value!r}")
    if ":" in normalized.split("/", 1)[0]:
        raise InvalidPathError(f"path nie moze zawierac drive-letter (np. C:/): {value!r}")
    if any(part == ".." for part in normalized.split("/")):
        raise InvalidPathError(f"path nie moze zawierac '..': {value!r}")
    if not normalized.lower().endswith(".md"):
        raise InvalidPathError(f"path musi konczyc sie na '.md', dostalismy {value!r}")
    if normalized.endswith("/") or "//" in normalized:
        raise InvalidPathError(f"path ma nieprawidlowa strukture: {value!r}")

    return normalized


class VaultOperations:
    """Cienka fasada nad ``VaultManager`` z walidacja preconditions per operacja.

    **Nie trzyma stanu.** Kazda metoda to czysta funkcja: argumenty → zapis do
    ``vault_manager`` → ``OperationReport``. Walidacja sciezki i preconditions
    dzieje sie **przed** I/O — jesli cokolwiek nie przejdzie, zwracamy
    ``ok=False`` bez dotykania dysku.

    **Idempotencja jest po stronie narzedzia.** ``VaultOperations.create``
    wolane dwa razy na tej samej sciezce:

    - pierwszy raz: ``ok=True``
    - drugi raz: ``ok=False, error="path exists"``

    Narzedzia same decyduja, czy ``ok=False`` to znaczacy blad dla modelu (zwykle
    tak — model ma widziec ``ERROR: path exists`` i poprawic sie).
    """

    def __init__(self, vault_manager: VaultManager) -> None:
        self._vm = vault_manager

    @property
    def vault_manager(self) -> VaultManager:
        """Zwraca wstrzyknienty ``VaultManager`` — dla narzedzi, ktore potrzebuja
        niskopoziomowego API (read_text, note_exists) na potrzeby walidacji.
        """

        return self._vm

    def create(self, path: str, content: str) -> OperationReport:
        """``create`` — nowa notatka, sciezka nie moze juz istniec w vaulcie.

        Walidacja:

        1. Sciezka przechodzi ``validate_relative_md_path``.
        2. Plik nie istnieje (``vault_manager.note_exists(path) is False``).
        3. ``content`` jest niepustym stringiem.

        Przy sukcesie wola ``VaultManager.create`` (ktory rowniez waliduje sciezke
        po swojej stronie — podwojna kontrola jest OK, bo daje spojny komunikat
        bledu niezaleznie od wejscia).
        """

        return self._do_operation("create", path, content)

    def update(self, path: str, content: str) -> OperationReport:
        """``update`` — pelne nadpisanie istniejacej notatki.

        Walidacja:

        1. Sciezka przechodzi ``validate_relative_md_path``.
        2. Plik **istnieje** (``vault_manager.note_exists(path) is True``).
        3. ``content`` jest niepustym stringiem.

        Przy sukcesie wola ``VaultManager.overwrite``.
        """

        return self._do_operation("update", path, content)

    def append(self, path: str, content: str) -> OperationReport:
        """``append`` — dopisanie do body istniejacej notatki.

        Walidacja:

        1. Sciezka przechodzi ``validate_relative_md_path``.
        2. Plik **istnieje**.
        3. ``content`` jest niepustym stringiem (dopisek NIE zawiera frontmattera).

        Przy sukcesie wola ``VaultManager.append`` (ktory sam wstawi separator
        ``\\n\\n`` zgodnie ze swoja logika).
        """

        return self._do_operation("append", path, content)

    def _do_operation(
        self,
        op: OperationType,
        path: str,
        content: str,
    ) -> OperationReport:
        """Wspolny pipeline walidacji + dispatchu do ``VaultManager``.

        Wyjatki z ``VaultManager`` lapiemy i mapujemy na ``OperationReport.ok=False``
        — narzedzia nie chca rzucac, tylko dostawac ``ToolResult(ok=False)``.
        """

        try:
            normalized = validate_relative_md_path(path)
        except InvalidPathError as exc:
            return OperationReport(ok=False, op=op, path=path, error=str(exc))

        if not isinstance(content, str) or content == "":
            return OperationReport(
                ok=False, op=op, path=normalized, error="content musi byc niepustym stringiem"
            )

        exists = self._vm.note_exists(normalized)

        if op == "create" and exists:
            return OperationReport(
                ok=False,
                op=op,
                path=normalized,
                error=f"path exists: {normalized!r} - uzyj 'update' lub 'append'",
            )
        if op in ("update", "append") and not exists:
            return OperationReport(
                ok=False,
                op=op,
                path=normalized,
                error=f"path does not exist: {normalized!r} - uzyj 'create'",
            )

        try:
            if op == "create":
                self._vm.create(normalized, content)
            elif op == "update":
                self._vm.overwrite(normalized, content)
            elif op == "append":
                self._vm.append(normalized, content)
            else:
                return OperationReport(
                    ok=False, op=op, path=normalized, error=f"Nieznana operacja: {op!r}"
                )
        except Exception as exc:
            logger.exception("VaultOperations.%s padlo dla %s", op, Path(normalized).as_posix())
            return OperationReport(
                ok=False,
                op=op,
                path=normalized,
                error=f"{type(exc).__name__}: {exc}",
            )

        return OperationReport(ok=True, op=op, path=normalized)


__all__ = [
    "InvalidPathError",
    "OperationReport",
    "OperationType",
    "VaultOperations",
    "validate_relative_md_path",
]
