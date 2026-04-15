"""Odczyt danych z repozytorium Git przez GitPython."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
import re
from typing import Any

from git import BadName, GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Repo

from src.git.models import CommitInfo, CommitStats, FileChange, ChangeType
from src.git.status_map import map_git_status


@dataclass(slots=True)
class _PatchSection:
    """Jedna sekcja patcha odpowiadajaca pojedynczemu plikowi."""

    path: str
    old_path: str | None
    diff_text: str


class GitReader:
    """Czyta z repo lekkie metadane i ciezsze diffy na zadanie."""

    _IGNORED_DIRS = {
        ".git",
        ".obsidian",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }
    _IGNORED_SUFFIXES = {".lock", ".pyc", ".pyo"}
    _BINARY_SUFFIXES = {
        ".7z",
        ".avi",
        ".bin",
        ".class",
        ".dll",
        ".doc",
        ".docx",
        ".eot",
        ".exe",
        ".gif",
        ".gz",
        ".ico",
        ".jar",
        ".jpeg",
        ".jpg",
        ".mp3",
        ".mp4",
        ".mov",
        ".otf",
        ".pdf",
        ".png",
        ".so",
        ".tar",
        ".ttf",
        ".wav",
        ".webm",
        ".webp",
        ".woff",
        ".woff2",
        ".zip",
    }
    def __init__(self, repo_path: str | Path) -> None:
        self.repo_path = Path(repo_path).expanduser().resolve()
        try:
            self.repo = Repo(self.repo_path, search_parent_directories=False)
        except (InvalidGitRepositoryError, NoSuchPathError) as exc:
            raise ValueError(f"Sciezka {self.repo_path} nie wskazuje na repozytorium Git.") from exc

        if self.repo.bare:
            raise ValueError(f"Repozytorium {self.repo_path} jest bare i nie ma working tree.")

    @classmethod
    def from_config(cls, config_path: str | Path) -> "GitReader":
        """Buduje GitReader z `config.yaml`."""

        from src.providers import load_config_dict

        cfg = load_config_dict(config_path)
        paths = cfg.get("paths")
        if not isinstance(paths, dict):
            raise ValueError("config: sekcja 'paths' musi byc mapa")

        repo_path = paths.get("project_repo")
        if not repo_path or not isinstance(repo_path, str):
            raise ValueError("config: paths.project_repo jest wymagane")

        return cls(repo_path=repo_path)

    def get_current_branch(self) -> str:
        return self.repo.active_branch.name if not self.repo.head.is_detached else "HEAD"

    def get_file_tree(self, ref: str = "HEAD") -> list[str]:
        """Zwraca liste plikow z danego refa po odfiltrowaniu szumu."""

        try:
            output = self.repo.git.ls_tree("-r", "--name-only", ref)
        except GitCommandError as exc:
            raise ValueError(f"Nie mozna odczytac drzewa plikow dla refa {ref!r}.") from exc

        paths = [
            path
            for path in (line.strip() for line in output.splitlines())
            if path and not self._should_ignore(path)
        ]
        return sorted(paths)

    def get_recent_commits(
        self,
        since: datetime | None,
        limit: int = 20,
    ) -> list[CommitInfo]:
        """Zwraca lekkie dane commitow: bez patchy, ale z lista zmienionych plikow."""

        if not self.repo.head.is_valid():
            return []

        iter_kwargs: dict[str, str | int] = {}
        if since is not None:
            iter_kwargs["since"] = since.isoformat()
        else:
            iter_kwargs["max_count"] = limit

        commits = list(self.repo.iter_commits("HEAD", **iter_kwargs))
        result: list[CommitInfo] = []
        for commit in commits:
            commit_info = self._build_commit_info(commit)
            if commit_info is not None:
                result.append(commit_info)
        return result

    def get_commit_diff(self, sha: str, path_filter: str | None = None) -> list[FileChange]:
        """Zwraca pelne diffy dla wskazanego commita."""

        self._ensure_commit_exists(sha)
        return self._load_commit_changes(sha=sha, include_diff=True, path_filter=path_filter)

    def get_commits_since_last_run(self, processed_shas: list[str]) -> list[CommitInfo]:
        """Zwraca nieprzetworzone commity z szerszego okna ostatniej historii."""

        processed_set = set(processed_shas)
        lookback_limit = max(50, len(processed_set) + 20)
        commits = self.get_recent_commits(since=None, limit=lookback_limit)
        return [commit for commit in commits if commit.sha not in processed_set]

    @classmethod
    def _should_ignore(cls, path: str) -> bool:
        posix_path = PurePosixPath(path)

        if any(part in cls._IGNORED_DIRS for part in posix_path.parts):
            return True

        name = posix_path.name
        if name == ".env" or name.startswith(".env."):
            return True

        suffix = posix_path.suffix.lower()
        if suffix in cls._IGNORED_SUFFIXES:
            return True

        if suffix in cls._BINARY_SUFFIXES:
            return True

        return False

    def _build_commit_info(self, commit: Any) -> CommitInfo | None:
        stats_total = commit.stats.total
        changes = self._load_commit_changes(sha=commit.hexsha, include_diff=False)
        if not changes:
            return None

        author = commit.author.name or commit.author.email or "unknown"
        return CommitInfo(
            sha=commit.hexsha,
            message=commit.message.strip(),
            author=author,
            date=commit.committed_datetime,
            changes=changes,
            stats=CommitStats(
                insertions=stats_total.get("insertions", 0),
                deletions=stats_total.get("deletions", 0),
            ),
        )

    def _ensure_commit_exists(self, sha: str) -> Any:
        try:
            return self.repo.commit(sha)
        except (BadName, ValueError, GitCommandError) as exc:
            raise ValueError(f"Commit {sha!r} nie istnieje w repozytorium.") from exc

    def _load_commit_changes(
        self,
        *,
        sha: str,
        include_diff: bool,
        path_filter: str | None = None,
    ) -> list[FileChange]:
        name_status_output = self._run_git_show(
            sha=sha,
            include_patch=False,
            path_filter=path_filter,
        )
        parsed_changes = self._parse_name_status_output(name_status_output)

        patch_sections = []
        if include_diff:
            patch_output = self._run_git_show(
                sha=sha,
                include_patch=True,
                path_filter=path_filter,
            )
            patch_sections = self._split_patch_sections(patch_output)

        changes: list[FileChange] = []
        for change_type, path, old_path in parsed_changes:
            if self._should_ignore(path):
                continue

            diff_text = ""
            if include_diff:
                diff_text = self._pop_matching_patch(
                    patch_sections=patch_sections,
                    path=path,
                    old_path=old_path,
                )

            changes.append(
                FileChange(
                    path=path,
                    change_type=change_type,
                    diff_text=diff_text,
                    old_path=old_path,
                )
            )

        return changes

    def _run_git_show(
        self,
        *,
        sha: str,
        include_patch: bool,
        path_filter: str | None = None,
    ) -> str:
        args = [
            sha,
            "--format=",
            "--find-renames",
            "--find-copies",
            "--first-parent",
        ]
        if include_patch:
            args.extend(["--patch", "--unified=3", "--no-ext-diff"])
        else:
            args.append("--name-status")

        if path_filter:
            args.extend(["--", path_filter])

        try:
            return self.repo.git.show(*args)
        except GitCommandError as exc:
            raise ValueError(f"Nie mozna odczytac danych dla commita {sha!r}.") from exc

    @staticmethod
    def _parse_name_status_output(
        output: str,
    ) -> list[tuple[ChangeType, str, str | None]]:
        changes: list[tuple[ChangeType, str, str | None]] = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            parts = line.split("\t")
            if len(parts) < 2:
                continue

            change_type = map_git_status(parts[0])
            if change_type in {ChangeType.RENAMED, ChangeType.COPIED} and len(parts) >= 3:
                old_path = parts[1]
                path = parts[2]
            else:
                old_path = None
                path = parts[1]

            changes.append((change_type, path, old_path))

        return changes

    @classmethod
    def _split_patch_sections(cls, patch_text: str) -> list[_PatchSection]:
        sections: list[str] = []
        current: list[str] = []

        for line in patch_text.splitlines(keepends=True):
            if line.startswith("diff --git "):
                if current:
                    sections.append("".join(current))
                current = [line]
                continue

            if current:
                current.append(line)

        if current:
            sections.append("".join(current))

        return [cls._parse_patch_section(section) for section in sections]

    @staticmethod
    def _parse_patch_section(section: str) -> _PatchSection:
        lines = section.splitlines()
        if not lines:
            return _PatchSection(path="", old_path=None, diff_text=section)

        match = re.match(r"^diff --git a/(.+) b/(.+)$", lines[0])
        if match:
            a_path, b_path = match.groups()
        else:
            a_path, b_path = "", ""

        old_path = None if a_path == "dev/null" else a_path
        path = b_path if b_path != "dev/null" else a_path

        for line in lines[1:]:
            if line.startswith("rename from ") or line.startswith("copy from "):
                old_path = line.split(" ", 2)[2]
            elif line.startswith("rename to ") or line.startswith("copy to "):
                path = line.split(" ", 2)[2]

        return _PatchSection(path=path, old_path=old_path, diff_text=section)

    @staticmethod
    def _pop_matching_patch(
        *,
        patch_sections: list[_PatchSection],
        path: str,
        old_path: str | None,
    ) -> str:
        for index, section in enumerate(patch_sections):
            if section.path == path and section.old_path == old_path:
                return patch_sections.pop(index).diff_text

        for index, section in enumerate(patch_sections):
            if section.path == path:
                return patch_sections.pop(index).diff_text
            if old_path is not None and section.old_path == old_path:
                return patch_sections.pop(index).diff_text

        return ""
