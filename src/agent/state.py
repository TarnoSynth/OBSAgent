"""Odczyt i zapis ``.agent-state.json`` \u2014 persystencja stanu agenta.

``AgentStateStore`` to cienka warstwa I/O nad ``AgentState``. Nie wie nic
o Gicie, vaulcie ani AI \u2014 tylko o pliku JSON, scieżce i limicie rozmiaru
listy przetworzonych commitow.

Kontrakty:

- ``load()`` zwraca ``AgentState | None`` \u2014 brak pliku = ``None``
  (decyzja "co zrobic na pierwszym biegu" zostaje agentowi)
- ``save(state)`` **atomowy zapis**: najpierw tymczasowy plik obok docelowego,
  potem ``os.replace`` \u2014 Ctrl+C w trakcie zapisu nie zepsuje istniejacego stanu
- ``save(state)`` trimuje ``processed_commits[repo]`` do ostatnich
  ``processed_window`` SHA per repo (domyslnie 20)
- Scieżka domyslna: ``<repo_agenta>/.agent-state.json``, nadpisywalna
  przez ``agent.state_file`` w ``config.yaml``

Polityka trim: trzymamy **pierwsze** N z listy (najnowsze na poczatku \u2014
``AgentState.mark_processed`` prependuje). Starsze SHA wypadaja. Jesli user
zwiekszy ``processed_commits_window`` w configu, bedzie to efektywne od
pierwszego nastepnego zapisu.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from src.agent.models import AgentState

logger = logging.getLogger(__name__)


DEFAULT_STATE_FILENAME = ".agent-state.json"
DEFAULT_PROCESSED_WINDOW = 20


class AgentStateStore:
    """Persystencja ``AgentState`` w pliku JSON.

    Uzywane przez agenta (Faza 6) na dwoch koncach biegu:

    - start: ``state = store.load()`` \u2014 odtworzenie kontekstu miedzy biegami
    - koniec: ``store.save(state)`` \u2014 zapis po wszystkich zaakceptowanych akcjach

    **Nie** odpowiada za decyzje co robic jesli stan nie istnieje \u2014
    zwraca ``None`` i tyle. Nie odpowiada tez za budowanie ``VaultSnapshot``
    (to robi ``VaultSnapshot.from_knowledge`` w ``models``).
    """

    def __init__(
        self,
        state_path: str | Path,
        *,
        processed_window: int = DEFAULT_PROCESSED_WINDOW,
    ) -> None:
        if processed_window < 1:
            raise ValueError(
                f"processed_window musi byc >= 1, dostalismy {processed_window!r}."
            )
        self.state_path = Path(state_path).expanduser().resolve()
        self.processed_window = processed_window

    @classmethod
    def from_config(cls, config_path: str | Path) -> "AgentStateStore":
        """Buduje ``AgentStateStore`` z ``config.yaml``.

        Odczytuje ``agent.state_file`` (opcjonalne, domyslnie
        ``<repo_agenta>/.agent-state.json``) i ``agent.processed_commits_window``
        (opcjonalne, domyslnie ``DEFAULT_PROCESSED_WINDOW``).
        """

        from src.providers import load_config_dict

        cfg_path = Path(config_path).expanduser().resolve()
        cfg = load_config_dict(cfg_path)
        agent_cfg = cfg.get("agent") or {}
        if not isinstance(agent_cfg, dict):
            raise ValueError("config: sekcja 'agent' musi byc mapa")

        raw_state_file = agent_cfg.get("state_file")
        if raw_state_file:
            if not isinstance(raw_state_file, str):
                raise ValueError("config: agent.state_file musi byc stringiem")
            state_path = Path(raw_state_file).expanduser()
            if not state_path.is_absolute():
                state_path = cfg_path.parent / state_path
        else:
            state_path = cfg_path.parent / DEFAULT_STATE_FILENAME

        window = agent_cfg.get("processed_commits_window", DEFAULT_PROCESSED_WINDOW)
        try:
            window_int = int(window)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "config: agent.processed_commits_window musi byc liczba calkowita"
            ) from exc

        return cls(state_path=state_path, processed_window=window_int)

    def exists(self) -> bool:
        """Czy plik stanu istnieje na dysku."""

        return self.state_path.is_file()

    def load(self) -> AgentState | None:
        """Wczytuje stan z pliku. Zwraca ``None`` gdy pliku nie ma.

        Rzuca ``ValueError`` przy uszkodzonym JSON-ie lub niezgodnym schemacie
        Pydantic \u2014 agent powinien zdecydowac: przerwac bieg, albo potraktowac
        jak pierwszy start (po wczesniejszym backupie zepsutego pliku).
        """

        if not self.state_path.is_file():
            logger.debug("Brak pliku stanu pod %s \u2014 zwracam None.", self.state_path)
            return None

        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(
                f"Nie mozna odczytac pliku stanu {self.state_path}: {exc}"
            ) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Uszkodzony JSON w pliku stanu {self.state_path}: {exc.msg} (linia {exc.lineno})"
            ) from exc

        try:
            return AgentState.model_validate(data)
        except ValidationError as exc:
            raise ValueError(
                f"Niezgodny schemat stanu w {self.state_path}: {exc}"
            ) from exc

    def save(self, state: AgentState) -> None:
        """Atomowy zapis stanu do pliku JSON.

        Wykonanie:

        1. Trim ``processed_commits[repo]`` do ostatnich ``processed_window`` SHA
        2. Serializacja ``AgentState`` do JSON-a (UTF-8, indent=2, ISO-8601 daty)
        3. Zapis do pliku tymczasowego **w tym samym folderze** co docelowy
           (``os.replace`` musi byc na tym samym systemie plikow)
        4. ``os.replace`` \u2014 atomowa podmiana. Jesli proces padnie miedzy
           krokami 3 i 4, stary plik stanu zostaje nietkniety.
        """

        trimmed = self._trim_processed_commits(state)
        payload = trimmed.model_dump_json(indent=2)

        self.state_path.parent.mkdir(parents=True, exist_ok=True)

        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=self.state_path.name + ".",
            suffix=".tmp",
            dir=str(self.state_path.parent),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.state_path)
            logger.debug("Zapisano stan agenta: %s", self.state_path)
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    logger.warning("Nie udalo sie usunac %s po bledzie zapisu.", tmp_path)
            raise

    def _trim_processed_commits(self, state: AgentState) -> AgentState:
        """Zwraca kopie stanu z listami ``processed_commits`` uciętymi do okna.

        Nie modyfikuje przekazanego ``state`` in-place \u2014 agent moze dalej
        trzymac oryginal w pamieci.
        """

        trimmed_commits = {
            repo: shas[: self.processed_window]
            for repo, shas in state.processed_commits.items()
        }
        return state.model_copy(update={"processed_commits": trimmed_commits})
