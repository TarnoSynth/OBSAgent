"""``ToolExecutionContext`` \u2014 stan dzielony miedzy wywolaniami narzedzi w jednej sesji agenta.

**Rola w architekturze:**

Kiedy model wchodzi w petle tool-use dla **jednego commita projektowego**,
wszystkie wywolania narzedzi (od pierwszego ``list_notes`` do koncowego
``submit_plan``) dziela ten sam context. Context niesie:

- **Zewnetrzne zasoby** (``vault_manager``, ``git_reader``, ``run_logger``)
  \u2014 narzedzia je czytaja/pisza.
- **Kontekst commita** (``commit_info``) \u2014 SHA, wiadomosc, lista plikow,
  diffy. ``get_commit_context`` tool bedzie to zwracalo modelowi.
- **Cache snapshotu vaulta** (``vault_knowledge``) \u2014 zbudowany raz per
  sesja przez ``VaultManager.scan_all``. Narzedzia eksploracyjne (``list_notes``,
  ``read_note``, ``find_related``) go czytaja. Gdy cos sie zapisze do
  vaulta (przez narzedzie write), context jest invalidowany \u2014 kolejne
  tools beda czytac swiezy stan.
- **Bufor wykonanych akcji** (``executed_actions``) \u2014 narzedzia write
  dopisuja tu strukturowane wpisy (typ, sciezka, co zrobiono). Po
  zakonczeniu sesji tym zasila sie ``PendingBatch`` i preview dla usera.
- **Flaga finalizacji** (``finalized`` + ``final_summary``) \u2014 ustawiana
  przez narzedzie ``submit_plan`` na sygnal zakonczenia sesji. Agent
  wychodzi z petli po jej wykryciu.

**Czego NIE ma w contextie:**

- Stanu AI providera (request/response, uzycie tokenow). To trzyma
  ``RunLogger`` + sam ``Agent``. Narzedzia nie ingeruja w konwersacje.
- Konfiguracji agenta (sciezek, max_retries, language). Gdy narzedzie
  potrzebuje konfiguracji, dostaje ja przy konstruktorze (``create_hub(..., default_tags=[...])``)
  albo z configu globalnego przez dedykowany getter \u2014 NIE przez context.

**Thread safety:**

Context NIE jest thread-safe. Jedna sesja agenta jest sekwencyjna:
model wola narzedzia, kazde uruchamiane po kolei, nawet gdy provider
zwroci parallel_tool_calls \u2014 ``ToolRegistry.dispatch`` je awaituje
po kolei. Gdyby kiedys zmienilismy model wywolan na concurrent, trzeba
bedzie zalozyc lock na invalidacji cache'a.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from logs.run_logger import RunLogger
    from src.agent.models_actions import ProposedWrite
    from src.git.models import CommitInfo
    from src.git.reader import GitReader
    from src.vault.manager import VaultManager
    from src.vault.models import VaultKnowledge


@dataclass
class ToolExecutionContext:
    """Stan sesji tool-use. Jeden obiekt per commit projektowy.

    Pola dzielone sa na cztery grupy:

    1. **Zasoby** (zawsze ustawione): ``vault_manager``.
    2. **Kontekst opcjonalny**: ``git_reader``, ``commit_info``, ``run_logger``.
       Gdy narzedzie ich potrzebuje i sa ``None``, zwraca ``ToolResult``
       z bledem \u2014 nie crashuje.
    3. **Bufory sesyjne**: ``executed_actions``, ``pending_concepts``.
       Rosna w czasie trwania sesji. Koncowy snapshot trafia do preview
       i PendingBatch.
    4. **Cache + kontrola petli**: ``vault_knowledge`` (lazy, invalidowany
       po write), ``finalized`` + ``final_summary`` (terminator).
    """

    # --- zasoby ---
    vault_manager: "VaultManager"

    # --- kontekst (opcjonalny) ---
    git_reader: "GitReader | None" = None
    commit_info: "CommitInfo | None" = None
    run_logger: "RunLogger | None" = None

    # --- bufory sesyjne ---
    executed_actions: list[dict[str, Any]] = field(default_factory=list)
    """Historia wykonanych tool-level zapisow w tej sesji.

    Kazdy wpis zawiera min.: ``{"tool": str, "path": str | None, "args": dict,
    "result": "ok" | "failed"}``. Uzywane przez ``PendingBatch`` (Faza 1)
    do zbudowania podgladu zmian dla usera.
    """

    pending_concepts: list[dict[str, Any]] = field(default_factory=list)
    """Lista zarejestrowanych placeholderow (orphan wikilinkow).

    Wpisywane przez ``register_pending_concept`` (Faza 5). Puste w Fazie 0
    \u2014 bufor gotowy na przyjscie tej funkcjonalnosci.
    """

    proposed_writes: list["ProposedWrite"] = field(default_factory=list)
    """Lista ``ProposedWrite`` **proponowanych** przez tools write w tej sesji.

    **Kluczowa semantyka Fazy 2 refaktoru:** narzedzia ``create_note`` /
    ``update_note`` / ``append_to_note`` NIE zapisuja do vaulta bezposrednio
    - one dopisuja swoja propozycje tutaj. Po wyjsciu z petli agent konwertuje
    ta liste na ``ProposedPlan`` i idzie standardowym torem ``apply_pending``
    -> preview -> ``finalize_pending`` / ``rollback_pending``.

    Dzieki temu:

    - zachowany diff-view (czerwone ``[!failure]+`` + zielone ``[!tip]+``)
    - zachowany user gating (``[T/n]``) zanim cokolwiek zostanie zacommitowane
    - tools sa **proponentami**, nie exekutorami - separacja koncepcyjna
      identyczna z tym, czym bylo ``submit_plan`` przed refactorem

    W Fazie 3+ eksploracyjne narzedzia (``read_note``, ``list_notes``,
    ``find_related``) czytaja vault realnie (przez ``vault_manager`` +
    ``ensure_vault_knowledge``), ale write-owe dalej tu rejestruja akcje.
    """

    # --- cache + terminator ---
    vault_knowledge: "VaultKnowledge | None" = None
    """Snapshot ``VaultKnowledge`` zbudowany lazy przy pierwszym uzyciu.

    Narzedzia eksploracyjne (Faza 3) czytaja z tego cache'a zamiast
    ponownie wolac ``vault_manager.scan_all()``. Po kazdej udanej operacji
    write invalidowany przez ``invalidate_vault_knowledge`` \u2014 kolejne
    reads dostaja swiezy stan.
    """

    finalized: bool = False
    """Ustawiane na ``True`` przez ``submit_plan`` tool. Agent sprawdza
    po kazdej turze \u2014 ``True`` = wyjscie z petli tool-use."""

    final_summary: str | None = None
    """Podsumowanie sesji przekazane w argumentach ``submit_plan``.
    Dostepne dla preview/logow po wyjsciu z petli."""

    # --- API ---

    def invalidate_vault_knowledge(self) -> None:
        """Zrzuca cache VaultKnowledge \u2014 kolejny tool read zbuduje swiezy.

        Wolane automatycznie przez narzedzia write po udanej operacji.
        Idempotentne.
        """

        self.vault_knowledge = None

    def ensure_vault_knowledge(self) -> "VaultKnowledge":
        """Zwraca snapshot vaulta z cache albo buduje go lazy.

        Tania sciezka \u2014 uzywaj w narzedziach eksploracyjnych zamiast
        ``vault_manager.scan_all()`` bezposrednio. Buduje tylko raz na
        sesje (a po write'ach znowu raz na kazdy "pakiet" zmian).
        """

        if self.vault_knowledge is None:
            self.vault_knowledge = self.vault_manager.scan_all()
        return self.vault_knowledge

    def record_action(
        self,
        *,
        tool: str,
        path: str | None,
        args: dict[str, Any],
        ok: bool,
        error: str | None = None,
    ) -> None:
        """Dopisuje wpis do ``executed_actions`` w ustalonym formacie.

        Helper dla narzedzi write, zeby nie powtarzaly tego samego
        ``dict`` budowania przy kazdym execute.
        """

        entry: dict[str, Any] = {
            "tool": tool,
            "path": path,
            "args": args,
            "result": "ok" if ok else "failed",
        }
        if error is not None:
            entry["error"] = error
        self.executed_actions.append(entry)

    def record_proposed_write(self, action: "ProposedWrite") -> None:
        """Dopisuje ``ProposedWrite`` do ``proposed_writes``.

        Wolane przez narzedzia write (``create_note`` / ``update_note`` /
        ``append_to_note``) po udanej walidacji. Narzedzia nie zapisuja
        bezposrednio do vaulta — ta lista jest konsumowana po wyjsciu z
        petli przez agenta (konwersja na ``ProposedPlan`` + ``apply_pending``).

        Idempotencja nie jest wymuszana — jesli model wola ``append_to_note``
        dwa razy z ta sama trescia, dostaniemy dwa wpisy. Tak tez powinno byc,
        bo ``ActionExecutor.apply_pending`` aplikuje akcje sekwencyjnie i
        dwa ``append`` daja dwukrotny dopisek (semantycznie poprawne).
        """

        self.proposed_writes.append(action)

    def has_pending_create(self, path: str) -> bool:
        """Sprawdza, czy w tej sesji zarejestrowano juz ``create`` na tej sciezce.

        Uzywane przez tools write do walidacji preconditions w sekwencji
        ``create_note("a.md")`` → ``update_note("a.md")``: drugi tool nie
        znajdzie pliku na dysku (nic jeszcze nie zapisalismy), ale *logicznie*
        on "istnieje" w kolejce propozycji, wiec update/append powinny przejsc.

        Porownanie sciezek po stringu — proposed_writes maja juz znormalizowane
        sciezki po walidacji Pydantic ``ProposedWrite._validate_path``.
        """

        return any(
            pw.type == "create" and pw.path == path for pw in self.proposed_writes
        )

    def finalize(self, summary: str) -> None:
        """Zamyka sesje \u2014 wolane wylacznie przez narzedzie ``submit_plan``.

        Ustawia ``finalized=True`` i zapisuje ``final_summary``. Idempotentne
        \u2014 drugi call nadpisuje summary (ostatni wygrywa), ale flaga juz
        True po pierwszym razie, wiec agent wyjdzie z petli.
        """

        self.finalized = True
        self.final_summary = summary


__all__ = ["ToolExecutionContext"]
