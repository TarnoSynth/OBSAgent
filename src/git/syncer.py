"""Synchronizacja dowolnego repo Git z remote (pull + auto-stash).

``GitSyncer`` jest agnostyczny — dziala tak samo dla repo projektu jak i dla
vaulta Obsidiana. Nie zna formatu plikow, frontmattera ani semantyki notatek.
Odpowiada wylacznie za operacje gitowe po stronie zapisu (pull z safe-stash).

Odczyt danych (commity, diffy, drzewo) realizuje ``GitReader``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from git import GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Repo

from src.git.exceptions import (
    GitSyncError,
    NoRemoteError,
    OfflineError,
    PullConflictError,
    StashError,
)

logger = logging.getLogger(__name__)

_STASH_MESSAGE = "agent-auto-stash"

_OFFLINE_HINTS = (
    "could not resolve host",
    "could not resolve",
    "name or service not known",
    "network is unreachable",
    "no route to host",
    "connection timed out",
    "connection refused",
    "operation timed out",
    "failed to connect",
    "unable to access",
    "ssl_connect",
    "temporary failure in name resolution",
    "bad gateway",
    "service unavailable",
)

_CONFLICT_HINTS = (
    "conflict",
    "automatic merge failed",
    "merge conflict",
    "needs merge",
    "unmerged",
    "would be overwritten",
)


class GitSyncer:
    """Pull + auto-stash na dowolnym repo Git.

    Przyklad:
        >>> GitSyncer("/path/to/vault").sync()
        >>> GitSyncer("/path/to/project").sync()
    """

    def __init__(self, repo_path: str | Path) -> None:
        self.repo_path = Path(repo_path).expanduser().resolve()
        if not self.repo_path.is_dir():
            raise ValueError(f"Sciezka {self.repo_path} nie istnieje lub nie jest katalogiem.")

        try:
            self._repo = Repo(self.repo_path, search_parent_directories=False)
        except (InvalidGitRepositoryError, NoSuchPathError) as exc:
            raise ValueError(
                f"Sciezka {self.repo_path} nie jest repozytorium Git. "
                f"Zainicjalizuj je: cd \"{self.repo_path}\" && git init"
            ) from exc

        if self._repo.bare:
            raise ValueError(f"Repozytorium {self.repo_path} jest bare — brak working tree.")

    def sync(self) -> None:
        """Synchronizuje repo z remote. Rzuca ``GitSyncError`` przy problemach."""

        if not self._repo.remotes or "origin" not in [r.name for r in self._repo.remotes]:
            raise NoRemoteError(
                f"Repo {self.repo_path} nie ma skonfigurowanego remote 'origin'. "
                f"Ustaw go: cd \"{self.repo_path}\" && git remote add origin <url>"
            )

        stashed = False
        if self._repo.is_dirty(untracked_files=True):
            logger.info("Repo %s ma niezacommitowane zmiany — stashuje przed pullem.", self.repo_path)
            try:
                self._repo.git.stash("push", "--include-untracked", "-m", _STASH_MESSAGE)
                stashed = True
            except GitCommandError as exc:
                raise GitSyncError(
                    f"Nie udalo sie wykonac `git stash` w repo {self.repo_path}. "
                    f"Sprawdz recznie: cd \"{self.repo_path}\" && git status"
                ) from exc

        branch = self._current_branch_name()
        try:
            self._repo.git.pull("origin", branch)
        except GitCommandError as exc:
            self._raise_pull_error(exc, branch=branch, stashed=stashed)

        if stashed:
            try:
                self._repo.git.stash("pop")
            except GitCommandError as exc:
                raise StashError(
                    f"`git stash pop` zwrocil konflikt w repo {self.repo_path}. "
                    "Twoje lokalne zmiany sa nadal bezpiecznie w stash. "
                    f"Rozwiaz recznie: cd \"{self.repo_path}\" && git status && git stash list"
                ) from exc

        logger.info("Repo %s zsynchronizowane z remote (branch=%s).", self.repo_path, branch)

    def _current_branch_name(self) -> str:
        if self._repo.head.is_detached:
            raise GitSyncError(
                f"Repo {self.repo_path} jest w stanie detached HEAD. "
                f"Przelacz sie na galaz: cd \"{self.repo_path}\" && git switch main"
            )
        return self._repo.active_branch.name

    @staticmethod
    def _raise_pull_error(exc: GitCommandError, *, branch: str, stashed: bool) -> None:
        message = f"{exc.stderr or ''} {exc.stdout or ''} {exc}".lower()
        if any(hint in message for hint in _OFFLINE_HINTS):
            raise OfflineError(
                "Brak polaczenia z remote. Sprawdz internet i sprobuj ponownie. "
                + ("Twoje zmiany sa w stash (git stash list)." if stashed else "")
            ) from exc

        if any(hint in message for hint in _CONFLICT_HINTS):
            raise PullConflictError(
                f"`git pull origin {branch}` zwrocil konflikt. "
                "Rozwiaz recznie w terminalu i zrob commit przed ponownym uruchomieniem."
                + (" Uwaga: twoje lokalne zmiany sa w stash." if stashed else "")
            ) from exc

        raise GitSyncError(
            f"Nieoczekiwany blad `git pull origin {branch}`: {exc}."
            + (" Uwaga: twoje lokalne zmiany sa w stash." if stashed else "")
        ) from exc
