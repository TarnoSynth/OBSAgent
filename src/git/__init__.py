"""Publiczne API warstwy Git.

Warstwa jest **agnostyczna** wzgledem tego, co repozytorium przechowuje:
dziala tak samo dla repo projektu (kod) i dla vaulta Obsidiana (notatki).
Komponujace decyzje (ktore repo czytac, co robic z wynikami) nalezy zostawic
warstwie agenta (``src/agent``).

- ``GitReader``  — odczyt commitow, diffow, drzewa plikow (readonly)
- ``GitSyncer``  — pull + auto-stash na dowolnym repo
- ``GitSyncError`` + podklasy — kontrakt bledow sync
"""

from src.git.exceptions import (
    GitSyncError,
    NoRemoteError,
    OfflineError,
    PullConflictError,
    StashError,
)
from src.git.models import ChangeType, CommitInfo, FileChange
from src.git.reader import GitReader
from src.git.syncer import GitSyncer

__all__ = [
    "ChangeType",
    "CommitInfo",
    "FileChange",
    "GitReader",
    "GitSyncError",
    "GitSyncer",
    "NoRemoteError",
    "OfflineError",
    "PullConflictError",
    "StashError",
]
