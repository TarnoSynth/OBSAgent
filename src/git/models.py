"""Modele danych dla warstwy integracji z Gitem."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ChangeType(StrEnum):
    """Typ zmiany pliku zwracany przez Git.

    Obejmuje kody spotykane w `git diff --name-status`, `git log --name-status`
    oraz dodatkowe statusy z `git status --porcelain`.
    """

    ADDED = "A"
    MODIFIED = "M"
    DELETED = "D"
    RENAMED = "R"
    COPIED = "C"
    TYPE_CHANGED = "T"
    UNMERGED = "U"
    UNKNOWN = "X"
    BROKEN_PAIRING = "B"
    UNTRACKED = "??"
    IGNORED = "!!"


class FileChange(BaseModel):
    """Opis pojedynczej zmiany pliku w commicie lub diffie.

    old_path jest ustawiane przy rename i copy.
    """

    path: str
    change_type: ChangeType
    diff_text: str = ""
    old_path: str | None = None


class CommitStats(BaseModel):
    """Podstawowe statystyki zmian w commicie."""

    insertions: int = 0
    deletions: int = 0


class CommitInfo(BaseModel):
    """Znormalizowany opis commita wraz z listą zmian."""

    sha: str
    message: str
    author: str
    date: datetime
    changes: list[FileChange] = Field(default_factory=list)
    stats: CommitStats = Field(default_factory=CommitStats)


class RepoContext(BaseModel):
    """Aktualny stan repozytorium potrzebny do dalszej analizy."""

    current_branch: str
    file_tree: list[str] = Field(default_factory=list)
    commit_count: int = 0
