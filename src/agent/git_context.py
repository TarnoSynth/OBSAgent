"""``GitContextBuilder`` \u2014 przygotowanie commita do analizy AI.

Zamienia ``CommitInfo`` (z pelnymi diffami z ``GitReader``) na
``ChunkedCommit`` gotowy do wyslania do AI \u2014 z pelnym chunkingiem
diffow per-hunk i filtrowaniem ignorowanych plikow.

**Rozroznienie**:

- ``src.git.GitReader`` \u2014 czyta Gita, nic nie wie o AI
- ``GitContextBuilder`` (tutaj) \u2014 decyduje co wysylamy, jak dzielimy,
  co pomijamy; nie zna jednak AI API (to juz w ``Agent``)
- ``Agent.propose_actions`` \u2014 decyduje CZY ``ChunkedCommit`` ma leciec
  jednym requestem (small) czy wieloma (multi-turn summary + finalize)

### Dlaczego zamienilismy truncate na chunking

Poprzednia wersja obcinala diff do ``max_diff_lines`` i dopisywala
marker ``... (diff obciety: +N linii)``. AI widzialo poczatek zmian
i musialo zgadywac reszte \u2014 przy duzych refactorach to dawalo
niepelna dokumentacje. Decyzja user (Q `chunk_strategy=per_hunk` +
`delivery=summarize_first`): chunkujemy per hunk, multi-turn per
chunk z podsumowaniami, finalny plan bazuje na zgromadzonych
podsumowaniach. Zaden fragment nie jest gubiony.

``max_diff_lines`` zachowuje nazwe, ale zmienia **znaczenie**: nie jest
juz tardem limitem obcinania, tylko **rozmiarem pojedynczego chunka**.
Wiecej linii = mniej chunkow = mniej requestow, ale wiecej tokenow
per request. Domyslne 300 sprawdza sie w typowych commitach.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

from pydantic import BaseModel

from src.agent.diff_chunker import chunk_commit
from src.agent.models_chunks import ChunkedCommit
from src.git.models import CommitInfo, FileChange


logger = logging.getLogger(__name__)


DEFAULT_MAX_DIFF_LINES = 300
DEFAULT_IGNORE_PATTERNS: tuple[str, ...] = (
    "*.lock",
    "*.pyc",
    "__pycache__/",
    "node_modules/",
    ".obsidian/",
    ".env",
)


class GitContextBuilder(BaseModel):
    """Chunkuje diffy commita i filtruje pliki wg ``ignore_patterns``.

    ``max_diff_lines`` to rozmiar pojedynczego chunka (domyslnie 300).
    Plik z diffem 1200 linii da 4 chunki. Pojedynczy hunk > 300 linii
    zostanie podzielony po liniach z powtorzonym headerem ``@@`` w
    kazdej czesci (decyzja user: ``big_hunk=split_lines``).

    ``ignore_patterns``: glob-style wzorce sprawdzane przeciw path.
    Dzialaja prefix-style dla folderow (``node_modules/`` pasuje do
    kazdej sciezki zawierajacej ``node_modules`` jako segment).
    """

    max_diff_lines: int = DEFAULT_MAX_DIFF_LINES
    ignore_patterns: tuple[str, ...] = DEFAULT_IGNORE_PATTERNS

    @classmethod
    def from_config(cls, config_path: str | Path) -> "GitContextBuilder":
        """Buduje z ``config.yaml``: ``agent.max_diff_lines`` + ``ignore_patterns``."""

        from src.providers import load_config_dict

        cfg = load_config_dict(Path(config_path).expanduser().resolve())

        agent_cfg = cfg.get("agent") or {}
        if not isinstance(agent_cfg, dict):
            raise ValueError("config: sekcja 'agent' musi byc mapa")

        raw_max = agent_cfg.get("max_diff_lines", DEFAULT_MAX_DIFF_LINES)
        try:
            max_diff_lines = int(raw_max)
        except (TypeError, ValueError) as exc:
            raise ValueError("config: agent.max_diff_lines musi byc liczba calkowita") from exc
        if max_diff_lines < 1:
            raise ValueError("config: agent.max_diff_lines musi byc >= 1")

        raw_patterns = cfg.get("ignore_patterns") or list(DEFAULT_IGNORE_PATTERNS)
        if not isinstance(raw_patterns, list):
            raise ValueError("config: ignore_patterns musi byc lista stringow")
        patterns: list[str] = []
        for p in raw_patterns:
            if not isinstance(p, str) or not p.strip():
                raise ValueError(f"config: ignore_patterns zawiera nieprawidlowy wpis: {p!r}")
            patterns.append(p.strip())

        return cls(max_diff_lines=max_diff_lines, ignore_patterns=tuple(patterns))

    def prepare_commit(self, commit: CommitInfo) -> ChunkedCommit:
        """Chunkuje **caly** diff commita, filtruje ignore, zwraca ``ChunkedCommit``.

        Flow:

        1. Filtruj ``commit.changes`` wg ``ignore_patterns`` \u2192
           ``filtered_changes`` (``skipped_files`` zachowujemy dla UI/preview).
        2. Wolamy ``chunk_commit(filtered_changes, max_diff_lines)`` \u2014
           ktory zbiera hunki wszystkich plikow razem i tnie na chunki
           po ``max_diff_lines`` linii. Chunki moga mieszac pliki.
        3. Zwracamy ``ChunkedCommit`` z lekkim ``commit`` (bez diffow
           w changes \u2014 diff "zyje" teraz w chunks).
        """

        filtered_changes: list[FileChange] = []
        skipped: list[str] = []
        lightweight_changes: list[FileChange] = []

        for change in commit.changes:
            if self._is_ignored(change.path):
                skipped.append(change.path)
                continue
            filtered_changes.append(change)
            lightweight_changes.append(change.model_copy(update={"diff_text": ""}))

        all_chunks = chunk_commit(filtered_changes, max_diff_lines=self.max_diff_lines)

        lightweight_commit = commit.model_copy(update={"changes": lightweight_changes})

        return ChunkedCommit(
            commit=lightweight_commit,
            chunks=all_chunks,
            skipped_files=skipped,
        )

    def _is_ignored(self, path: str) -> bool:
        if not path:
            return True
        normalized = path.replace("\\", "/")
        for pattern in self.ignore_patterns:
            if self._matches(normalized, pattern):
                return True
        return False

    @staticmethod
    def _matches(path: str, pattern: str) -> bool:
        """Dopasowanie glob-style z obsluga folderow ``foo/``.

        - ``*.lock``        \u2192 fnmatch na nazwie i pelnej sciezce
        - ``node_modules/`` \u2192 pasuje gdy `node_modules` jest dowolnym
          elementem sciezki (prefix, srodek, suffix)
        - Inne wzorce       \u2192 fnmatch na pelnej sciezce
        """

        if pattern.endswith("/"):
            folder = pattern[:-1]
            parts = path.split("/")
            return folder in parts

        if fnmatch.fnmatch(path, pattern):
            return True
        name = path.rsplit("/", 1)[-1]
        return fnmatch.fnmatch(name, pattern)


__all__ = ["DEFAULT_IGNORE_PATTERNS", "DEFAULT_MAX_DIFF_LINES", "GitContextBuilder"]
