"""Przygotowanie pelnych danych gitowych do bezpiecznego przekazania do AI."""

from __future__ import annotations

from pathlib import Path

from src.git.models import CommitInfo, FileChange


class GitContextBuilder:
    """Buduje odchudzone kopie danych pod prompt bez modyfikacji danych z readera."""

    def __init__(self, *, max_diff_lines: int) -> None:
        self.max_diff_lines = max(1, int(max_diff_lines))

    @classmethod
    def from_config(cls, config_path: str | Path) -> "GitContextBuilder":
        """Buduje konfiguracje limitow kontekstu AI z `config.yaml`."""

        from src.providers import load_config_dict

        cfg = load_config_dict(config_path)
        git_cfg = cfg.get("git", {})
        if git_cfg is None:
            git_cfg = {}
        if not isinstance(git_cfg, dict):
            raise ValueError("config: sekcja 'git' musi byc mapa")

        max_diff_lines_raw = git_cfg.get("max_diff_lines")
        if max_diff_lines_raw is None:
            raise ValueError("config: git.max_diff_lines jest wymagane")

        try:
            max_diff_lines = int(max_diff_lines_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("config: git.max_diff_lines musi byc liczba calkowita") from exc

        return cls(max_diff_lines=max_diff_lines)

    def prepare_file_changes(self, changes: list[FileChange]) -> list[FileChange]:
        """Zwraca kopie zmian dopasowane do budzetu promptu."""

        prepared: list[FileChange] = []
        for change in changes:
            prepared.append(
                change.model_copy(
                    update={"diff_text": self._truncate_diff(change.diff_text, self.max_diff_lines)}
                )
            )
        return prepared

    def prepare_commit(self, commit: CommitInfo) -> CommitInfo:
        """Zwraca kopie commita gotowa do przekazania do AI."""

        return commit.model_copy(update={"changes": self.prepare_file_changes(commit.changes)})

    @staticmethod
    def _truncate_diff(diff_text: str, max_lines: int) -> str:
        if not diff_text:
            return diff_text

        lines = diff_text.splitlines()
        if len(lines) <= max_lines:
            return diff_text

        kept_lines = lines[:max_lines]
        omitted_lines = len(lines) - max_lines
        kept_lines.append(f"(...obcieto {omitted_lines} linii)")
        return "\n".join(kept_lines) + "\n"
