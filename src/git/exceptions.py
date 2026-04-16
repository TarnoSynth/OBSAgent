"""Wyjatki warstwy Git.

Wszystkie bledy operacji na repozytorium (sync, pull, stash, brak remote,
problemy sieciowe) sa instancjami ``GitSyncError`` — warstwa wyzsza lapie je
jednym except-em. Komunikaty po polsku zawieraja konkretna instrukcje
dla usera.

Wyjatki sa agnostyczne wzgledem tego, co repo przechowuje: dzialaja tak samo
dla repo projektu jak i dla vaulta Obsidiana.
"""

from __future__ import annotations


class GitSyncError(Exception):
    """Bazowy wyjatek synchronizacji repo. Kazdy blad sync musi byc jego instancja."""


class NoRemoteError(GitSyncError):
    """Repo nie ma skonfigurowanego zdalnego ``origin``."""


class OfflineError(GitSyncError):
    """Brak polaczenia z remote (np. could not resolve host, timeout)."""


class PullConflictError(GitSyncError):
    """``git pull`` zwrocil konflikt — wymaga recznego rozwiazania przez usera."""


class StashError(GitSyncError):
    """``git stash pop`` po pullu nie powiodl sie. Zmiany usera pozostaja w stash."""
