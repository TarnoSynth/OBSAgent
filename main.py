"""Krótki przykład użycia providerów z tool callingiem.

Uruchom:
    python main.py

Przed uruchomieniem ustaw w `config.yaml` wybranego providera, np. `openai`,
oraz odpowiedni klucz API w `.env`.

Warstwa Git (`GitReader`, `GitContextBuilder`) przy starcie wypisuje pelny raport
tekstowy na stdout (gałąź, drzewo plikow, lista commitow, diffy, filtr since_last),
potem asercje; repozytorium: katalog z `main.py`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.agent import GitContextBuilder
from src.git.models import CommitInfo, CommitStats, FileChange
from src.git.reader import GitReader
from src.providers import (
    BaseProvider,
    ChatMessage,
    ChatRequest,
    MessageRole,
    ToolDefinition,
    ToolFunctionDefinition,
    build_provider,
)


def _short_preview(text: str, *, max_lines: int = 12) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text if text else "(pusty)"
    head = "\n".join(lines[:max_lines])
    return f"{head}\n... (+{len(lines) - max_lines} linii)"


def _run_git_reader_smoke_test(*, config_path: Path, repo_path: Path) -> None:
    """Sprawdza zwrotki GitReader i GitContextBuilder: asercje + pelny wydruk wynikow na stdout."""

    print()
    print("=" * 72)
    print("  GIT READER — wyniki sprawdzenia (czytelny raport)")
    print("=" * 72)
    print(f"repo_path: {repo_path.resolve()}")
    print(f"config:    {config_path.resolve()}")
    print()

    reader = GitReader(repo_path)
    branch = reader.get_current_branch()
    assert isinstance(branch, str) and branch.strip() != "", "get_current_branch: pusty string"
    print(f"[get_current_branch] -> {branch!r}")
    print()

    tree = reader.get_file_tree()
    assert isinstance(tree, list), "get_file_tree: oczekiwano list[str]"
    assert all(isinstance(p, str) for p in tree), "get_file_tree: elementy musza byc str"
    assert tree == sorted(tree), "get_file_tree: lista powinna byc posortowana"
    assert any(p.replace("\\", "/").endswith("main.py") for p in tree), (
        "get_file_tree: brak main.py — czy repo_path wskazuje na ten projekt?"
    )
    print(f"[get_file_tree] liczba plikow (po filtrze): {len(tree)}")
    print("  pierwsze 15 sciezek:")
    for p in tree[:15]:
        print(f"    - {p}")
    if len(tree) > 15:
        print(f"    ... i jeszcze {len(tree) - 15} plikow")
    print()

    recent = reader.get_recent_commits(since=None, limit=5)
    assert isinstance(recent, list), "get_recent_commits: oczekiwano list[CommitInfo]"
    print(f"[get_recent_commits(limit=5)] zwrocono {len(recent)} commitow")
    for i, item in enumerate(recent, start=1):
        assert isinstance(item, CommitInfo), "get_recent_commits: element musi byc CommitInfo"
        assert isinstance(item.sha, str) and len(item.sha) >= 7, "CommitInfo.sha"
        assert isinstance(item.message, str), "CommitInfo.message"
        assert isinstance(item.author, str) and item.author != "", "CommitInfo.author"
        assert isinstance(item.date, datetime), "CommitInfo.date"
        assert isinstance(item.stats, CommitStats), "CommitInfo.stats"
        assert item.stats.insertions >= 0 and item.stats.deletions >= 0, "CommitInfo.stats liczby"
        assert isinstance(item.changes, list), "CommitInfo.changes"
        msg_first = item.message.strip().split("\n", 1)[0][:80]
        print(f"  --- commit #{i} ---")
        print(f"  sha:     {item.sha}")
        print(f"  data:    {item.date.isoformat()}")
        print(f"  autor:   {item.author}")
        print(f"  wiadomosc (1. linia): {msg_first!r}")
        print(
            f"  stats:   +{item.stats.insertions} / -{item.stats.deletions} "
            f"(insertions / deletions)"
        )
        print(f"  pliki ({len(item.changes)}) — bez diffow (lekkie):")
        for ch in item.changes:
            assert isinstance(ch, FileChange), "zmiana musi byc FileChange"
            assert isinstance(ch.path, str) and ch.path != "", "FileChange.path"
            assert ch.diff_text == "", "get_recent_commits: lekkie commity bez diff_text"
            extra = f" <- {ch.old_path!r}" if ch.old_path else ""
            print(f"    [{ch.change_type.value}] {ch.path}{extra}")
        print()

    if recent:
        first_sha = recent[0].sha
        diffs = reader.get_commit_diff(first_sha)
        assert isinstance(diffs, list), "get_commit_diff: oczekiwano list[FileChange]"
        print(f"[get_commit_diff({first_sha[:7]}...)] plikow z patchem: {len(diffs)}")
        for fc in diffs:
            assert isinstance(fc, FileChange), "get_commit_diff: element FileChange"
            assert isinstance(fc.diff_text, str), "FileChange.diff_text musi byc str"
            n_lines = len(fc.diff_text.splitlines()) if fc.diff_text else 0
            print(f"  --- {fc.path} ---")
            print(f"  typ: {fc.change_type.value}  |  linii w diffie: {n_lines}")
            if fc.old_path:
                print(f"  stara sciezka: {fc.old_path}")
            print("  podglad diffa:")
            for line in _short_preview(fc.diff_text, max_lines=14).splitlines():
                print(f"    {line}")
            print()

        since_last = reader.get_commits_since_last_run([first_sha])
        assert isinstance(since_last, list), "get_commits_since_last_run: lista"
        assert all(c.sha != first_sha for c in since_last), (
            "get_commits_since_last_run: przetworzony sha nie powinien sie powtorzyc"
        )
        print(
            "[get_commits_since_last_run(processed=[najnowszy_sha])] "
            f"bez przetworzonego: {len(since_last)} commitow"
        )
        for j, c in enumerate(since_last[:8], start=1):
            one = c.message.strip().split("\n", 1)[0][:60]
            print(f"  {j}. {c.sha[:7]}...  {one!r}")
        if len(since_last) > 8:
            print(f"  ... i jeszcze {len(since_last) - 8}")
        print()

        builder = GitContextBuilder.from_config(config_path)
        prepared = builder.prepare_commit(recent[0])
        assert isinstance(prepared, CommitInfo), "prepare_commit: CommitInfo"
        assert prepared.sha == recent[0].sha
        print(
            f"[GitContextBuilder.prepare_commit] max_diff_lines={builder.max_diff_lines} "
            "(kopie pod AI — obciete diffy)"
        )
        for fc in prepared.changes:
            line_count = len(fc.diff_text.splitlines()) if fc.diff_text else 0
            assert line_count <= builder.max_diff_lines + 1, (
                "po obcieciu diff nie powinien przekroczyc max_diff_lines (+ ewentualna linia z komunikatem)"
            )
            print(f"  {fc.path}: {line_count} linii w diff_text (po obcieciu)")
        print()

    print("=" * 72)
    print("  Koniec raportu Git — asercje przeszly, dane powyzej to faktyczne zwrotki API.")
    print("=" * 72)
    print()


def _tool_echo(*, text: str) -> str:
    return text


def _safe_json_loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


async def _run_tool_demo(provider: BaseProvider) -> None:
    """Minimalny przepływ: assistant -> tool -> assistant."""
    # Minimalne narzedzie do testu E2E tool callingu.
    tools = [
        ToolDefinition(
            function=ToolFunctionDefinition(
                name="echo",
                description="Zwraca przekazany tekst (narzedzie testowe).",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            )
        )
    ]

    print("provider:", provider.name)
    messages: list[ChatMessage] = [
        ChatMessage(
            role=MessageRole.USER,
            content=(
                "Uzyj narzedzia echo z argumentem text='hello from tool'. "
                "Potem odpowiedz jednym zdaniem, co zwrocilo narzedzie."
            ),
        )
    ]

    # 1) Prosba o tool calls
    request = ChatRequest(
        messages=messages,
        tools=tools,
        tool_choice="auto",
        parallel_tool_calls=False,
    )
    result = await provider.complete(request)

    print("model:", result.model)
    if result.tool_calls:
        print("tool_calls:", [tc.function.name for tc in result.tool_calls])
    if result.text:
        print("assistant_text(pre):", result.text)

    # 2) Wykonanie narzedzi + odeslanie wynikow jako role=tool
    if result.tool_calls:
        messages.append(
            ChatMessage(
                role=MessageRole.ASSISTANT,
                content=result.text or None,
                tool_calls=result.tool_calls,
            )
        )

        for tc in result.tool_calls:
            args = _safe_json_loads(tc.function.arguments)
            if tc.function.name == "echo":
                text = args.get("text", "")
                tool_out = _tool_echo(text=str(text))
            else:
                tool_out = f"Nieznane narzedzie: {tc.function.name}"

            messages.append(
                ChatMessage(
                    role=MessageRole.TOOL,
                    tool_call_id=tc.id,
                    content=tool_out,
                )
            )

        # 3) Finalna odpowiedz po wynikach tooli
        followup = await provider.complete(
            ChatRequest(messages=messages, tools=tools, tool_choice="auto")
        )
        print("assistant_text(final):", followup.text)
        print("finish_reason:", followup.finish_reason)
        print("model(final):", followup.model)
    else:
        print("assistant_text:", result.text)
        print("finish_reason:", result.finish_reason)


async def main() -> None:
    project_root = Path(__file__).resolve().parent
    cfg = project_root / "config.yaml"
    _run_git_reader_smoke_test(config_path=cfg, repo_path=project_root)

    provider = build_provider(cfg)
    await _run_tool_demo(provider)


if __name__ == "__main__":
    asyncio.run(main())
