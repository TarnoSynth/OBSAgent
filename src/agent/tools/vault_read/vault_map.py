"""``vault_map`` — strukturalna mapa podgrafu vaulta (Faza 5 refaktoru: nawigacja po metadanych).

**Semantyka:**

Zwraca drzewo ``parent -> children`` w zwiezlej, tekstowej formie. Gdy
``root`` podany — startujemy od tej notatki (zwykle MOC lub hub) i
schodzimy do ``depth`` poziomow w dol. Gdy ``root=None`` — wypisujemy
wszystkie MOC-i top-level z ich bezposrednimi dziecmi.

Per notatka w drzewie: ``stem``, ``type``, a gdy ``include_tags=true``
rowniez ``tags``. Format wyjscia: markdown-friendly drzewo + structured
JSON w ``structured``.

**Po co to jest jako osobne narzedzie:**

Model, chcac "zorientowac sie w hierarchii" bez tego narzedzia, musi
zrobic:

1. ``list_notes(type='MOC')``                 — pobierz MOC-i
2. ``list_notes(parent='MOC__X')``            — dzieci MOC-a #1
3. ``list_notes(parent='MOC__Y')``            — dzieci MOC-a #2
4. ``list_notes(parent='Hub__Z')``            — wnuki

Czyli 4 + iteracje (jedna tura LLM per wywolanie). ``vault_map`` robi
to samo w 1 wywolaniu, a w jednym stringu pokazuje hierarchie gotowa do
zrozumienia "gdzie sie dowiazac".

**Koszt tokenowy:**

Dla typowego podgrafu (1 MOC + 5 hubow + 20 modulow) to ~1-2 kB. Limit
``_MAX_NODES`` twardo cape'uje rozmiar drzewa — powyzej tego model dostaje
komunikat "drzewo przyciete, zaweza ``root`` lub ``depth``".

**Determinizm:**

Wszystkie liste dzieci sortowane alfabetycznie po stem — daje stabilny
prefiks dla prompt cachingu miedzy biegami.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext
from src.vault.models import VaultKnowledge

_DEFAULT_DEPTH = 2
_MAX_DEPTH = 5
_MAX_NODES = 200


class _VaultMapArgs(BaseModel):
    """Schemat argumentow ``vault_map``."""

    model_config = ConfigDict(extra="forbid")

    root: str | None = Field(
        default=None,
        description=(
            "Korzen podgrafu — stem (np. 'MOC__Backend') lub wikilink ('[[MOC__Backend]]'). "
            "Gdy None: listujemy wszystkie MOC-i top-level i ich bezposrednie dzieci."
        ),
    )
    depth: int = Field(
        default=_DEFAULT_DEPTH,
        ge=1,
        le=_MAX_DEPTH,
        description=(
            f"Glebokosc rekursji w dol (ile poziomow dzieci). Domyslnie "
            f"{_DEFAULT_DEPTH}, maks {_MAX_DEPTH}. 1 = tylko bezposrednie dzieci."
        ),
    )
    include_tags: bool = Field(
        default=True,
        description=(
            "Gdy True — przy kazdym wezle drzewa pokaz tagi (pomaga wybrac "
            "wlasciwa galaz). Wylacz gdy chcesz maksymalnie zwiezla mape."
        ),
    )


class VaultMapTool(Tool):
    """Zwraca strukturalna mape podgrafu vaulta (MOC -> huby -> moduly)."""

    name = "vault_map"
    description = (
        "Drzewo hierarchii notatek: parent -> children (do wskazanej glebokosci). "
        "Gdy root=None: lista MOC-ow z bezposrednimi dziecmi. Gdy root=<stem>: "
        "podgraf od tego wezla. Zamienia 4-8 wywolan list_notes(parent=...) na "
        "jedno. Per wezel: stem, type i opcjonalnie tagi."
    )

    def input_schema(self) -> dict[str, Any]:
        return _VaultMapArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _VaultMapArgs.model_validate(args)
        except Exception as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        knowledge = ctx.ensure_vault_knowledge()

        roots = _resolve_roots(knowledge, parsed.root)
        if not roots:
            return ToolResult(
                ok=False,
                error=(
                    f"Nie znaleziono wezla root={parsed.root!r}. Uzyj stem notatki "
                    f"(np. 'MOC__Backend') albo root=None dla listy MOC-ow."
                ),
            )

        visited: set[str] = set()
        nodes_budget = [_MAX_NODES]

        tree_blocks: list[str] = []
        structured_roots: list[dict[str, Any]] = []
        for root_stem in roots:
            if nodes_budget[0] <= 0:
                break
            tree_lines: list[str] = []
            root_node = _build_subtree(
                knowledge=knowledge,
                stem=root_stem,
                depth_remaining=parsed.depth,
                include_tags=parsed.include_tags,
                visited=visited,
                nodes_budget=nodes_budget,
                lines=tree_lines,
                indent=0,
            )
            if tree_lines:
                tree_blocks.append("\n".join(tree_lines))
            if root_node is not None:
                structured_roots.append(root_node)

        truncated = nodes_budget[0] <= 0

        scope = parsed.root or "(top-level MOCs)"
        header = f"vault_map: root={scope}, depth={parsed.depth}"
        if truncated:
            header += f" (truncated — przekroczony limit {_MAX_NODES} wezlow)"
        if not tree_blocks:
            body = "(brak dzieci)"
        else:
            body = "\n\n".join(tree_blocks)
        content = f"{header}\n\n{body}"

        ctx.record_action(
            tool=self.name,
            path=None,
            args={
                "root": parsed.root,
                "depth": parsed.depth,
                "include_tags": parsed.include_tags,
                "nodes_returned": _MAX_NODES - nodes_budget[0],
                "truncated": truncated,
            },
            ok=True,
        )

        return ToolResult(
            ok=True,
            content=content,
            structured={
                "root": parsed.root,
                "depth": parsed.depth,
                "nodes": structured_roots,
                "truncated": truncated,
            },
        )


def _resolve_roots(knowledge: VaultKnowledge, root: str | None) -> list[str]:
    """Zwraca liste stemow, od ktorych zaczyna sie rekursja.

    Gdy ``root`` jest podane — jeden element, jesli da sie go rozwiazac
    przez ``knowledge.resolve`` albo gdy istnieje jako parent w indeksie.
    Gdy ``root`` nie jest konkretna notatka (tylko logicznym wezlem,
    np. tagiem-jak-nazwa), probujemy ``children_index`` bezposrednio.

    Gdy ``root=None`` — lista MOC-ow posortowana alfabetycznie.
    """

    if root is None:
        mocs = knowledge.mocs()
        return sorted({Path(m.path).stem for m in mocs})

    normalized = knowledge._normalize_ref(root)
    if not normalized:
        return []

    resolved = knowledge.resolve(root)
    if resolved is not None:
        return [Path(resolved.path).stem]

    if normalized in knowledge.children_index:
        return [normalized]

    return []


def _build_subtree(
    *,
    knowledge: VaultKnowledge,
    stem: str,
    depth_remaining: int,
    include_tags: bool,
    visited: set[str],
    nodes_budget: list[int],
    lines: list[str],
    indent: int,
) -> dict[str, Any] | None:
    """Rekurencyjnie buduje drzewo + rownolegle rysuje je do ``lines`` (markdown).

    ``nodes_budget[0]`` dekrementujemy po kazdym dodanym wezle, zeby twardo
    capowac ogromne poddrzewa. ``visited`` zapobiega cyklom w rzadkich
    przypadkach, gdy ktos recznie stworzyl parent kierujacy w gore.
    """

    if nodes_budget[0] <= 0:
        return None
    if stem in visited:
        return None
    visited.add(stem)

    note = knowledge.resolve(stem)
    node_type = note.type if note else None
    node_tags = list(note.tags) if note and note.tags else []
    node_path = note.path if note else None

    children_notes = knowledge.children_of(stem)
    child_count = len(children_notes)

    prefix = "  " * indent + "- "
    type_marker = f" _(type: {node_type})_" if node_type else ""
    tag_marker = f" [tags: {', '.join(node_tags)}]" if include_tags and node_tags else ""
    count_marker = f" — {child_count} dzieci" if child_count > 0 else ""
    lines.append(f"{prefix}`[[{stem}]]`{type_marker}{tag_marker}{count_marker}")
    nodes_budget[0] -= 1

    structured_node: dict[str, Any] = {
        "stem": stem,
        "path": node_path,
        "type": node_type,
        "child_count": child_count,
    }
    if include_tags:
        structured_node["tags"] = node_tags
    structured_children: list[dict[str, Any]] = []

    if depth_remaining <= 1 or child_count == 0:
        if child_count > 0 and depth_remaining <= 1:
            more_prefix = "  " * (indent + 1) + "- "
            lines.append(f"{more_prefix}_... ({child_count} dzieci — podnies 'depth' lub zmien 'root')_")
        structured_node["children"] = structured_children
        return structured_node

    sorted_children = sorted(children_notes, key=lambda n: n.path)
    for child_note in sorted_children:
        if nodes_budget[0] <= 0:
            break
        child_stem = Path(child_note.path).stem
        child_struct = _build_subtree(
            knowledge=knowledge,
            stem=child_stem,
            depth_remaining=depth_remaining - 1,
            include_tags=include_tags,
            visited=visited,
            nodes_budget=nodes_budget,
            lines=lines,
            indent=indent + 1,
        )
        if child_struct is not None:
            structured_children.append(child_struct)

    structured_node["children"] = structured_children
    return structured_node


__all__ = ["VaultMapTool"]
