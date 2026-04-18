"""``render_module`` — renderer notatki typu module (Faza 5).

Modul w naszym vaulcie to wezel dokumentacji pojedynczego modulu kodu
(pakiet / serwis / istotny komponent). Faza 5 dziedziczy strukture ze
staremego ``templates/module.md``, ale zamiast placeholderow
``{{title}}`` model przekazuje **strukturowane pola** i renderer sklada
je deterministycznie.

Stale sekcje (w kolejnosci):

1. **Streszczenie (prolog)** — 1 zdanie "za co odpowiada".
2. **``## Odpowiedzialnosc``** — lista bulletow.
3. **``## Kluczowe elementy``** — tabela ``| Element | Opis |``.
4. **``## Zaleznosci``** — dwie podsekcje inline: "Uzywa", "Jest uzywany przez".
5. **``## Kontrakty / API``** — opcjonalnie (sygnatury / endpointy).
6. **``## Decyzje architektoniczne``** — opcjonalnie, bullet list wikilinkow
   do ADR-ow / decision notes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.agent.tools.renderers._frontmatter import build_frontmatter
from src.agent.tools.renderers.concept import _inline_cell

__all__ = ["ModuleElement", "render_module"]


@dataclass(slots=True, frozen=True)
class ModuleElement:
    """Wpis w tabeli "## Kluczowe elementy".

    :ivar name: nazwa elementu (klasa/funkcja/endpoint) — zwykle w backtickach.
    :ivar description: jedno-dwuzdaniowy opis.
    """

    name: str
    description: str


def render_module(
    *,
    title: str,
    responsibility_summary: str,
    responsibilities: Sequence[str],
    key_elements: Sequence[ModuleElement],
    uses: Sequence[str],
    used_by: Sequence[str],
    parent: str | None,
    related: Sequence[str] | None,
    tags: Sequence[str] | None,
    created: str,
    updated: str | None = None,
    status: str | None = None,
    contracts_api: str | None = None,
    decisions: Sequence[str] | None = None,
) -> str:
    """Sklada pelny markdown notatki typu ``module``.

    :param title: ``# {title}``.
    :param responsibility_summary: 1-zdaniowy prolog (''Odpowiada za X'').
    :param responsibilities: lista bulletow dla sekcji "## Odpowiedzialnosc".
    :param key_elements: lista ``ModuleElement`` → tabela.
    :param uses: lista wikilinkow (``"[[Y]]"`` lub ``"Y"``) — moduly/technologie
        z ktorych TEN modul korzysta.
    :param used_by: lista wikilinkow — moduly ktore korzystaja z TEGO.
    :param parent, related, tags, created, updated, status: jak w rendererach.
    :param contracts_api: body sekcji "## Kontrakty / API" (markdown). ``None``
        albo pusty → sekcja pominieta.
    :param decisions: lista wikilinkow ADR / decision notes dla sekcji
        "## Decyzje architektoniczne". ``None`` / pusta → sekcja pominieta.
    """

    if not title.strip():
        raise ValueError("title module musi byc niepustym stringiem")
    if not responsibility_summary.strip():
        raise ValueError("responsibility_summary module musi byc niepustym stringiem")
    if not responsibilities:
        raise ValueError("responsibilities module musi miec co najmniej 1 wpis")
    if not key_elements:
        raise ValueError("key_elements module musi miec co najmniej 1 wpis")

    fm = build_frontmatter(
        note_type="module",
        tags=tags,
        parent=parent,
        related=related,
        status=status,
        created=created,
        updated=updated,
    )

    parts: list[str] = [
        f"# {title.strip()}",
        "",
        responsibility_summary.strip(),
        "",
        "## Odpowiedzialnosc",
        "",
    ]
    for item in responsibilities:
        if not isinstance(item, str) or not item.strip():
            continue
        parts.append(f"- {item.strip()}")
    parts.append("")

    parts.append("## Kluczowe elementy")
    parts.append("")
    parts.append("| Element | Opis |")
    parts.append("|---|---|")
    for el in key_elements:
        if not isinstance(el, ModuleElement):
            raise TypeError("key_elements musi byc Sequence[ModuleElement]")
        parts.append(f"| {_inline_cell(el.name)} | {_inline_cell(el.description)} |")
    parts.append("")

    parts.append("## Zaleznosci")
    parts.append("")
    parts.append(f"- **Uzywa:** {_format_wikilink_list(uses)}")
    parts.append(f"- **Jest uzywany przez:** {_format_wikilink_list(used_by)}")
    parts.append("")

    if contracts_api and contracts_api.strip():
        parts.append("## Kontrakty / API")
        parts.append("")
        parts.append(contracts_api.strip())
        parts.append("")

    if decisions:
        parts.append("## Decyzje architektoniczne")
        parts.append("")
        for wl in decisions:
            if not isinstance(wl, str) or not wl.strip():
                continue
            parts.append(f"- {_wrap_wikilink(wl)}")
        parts.append("")

    body = "\n".join(parts).rstrip() + "\n"
    return fm + "\n" + body


def _wrap_wikilink(raw: str) -> str:
    value = raw.strip()
    if value.startswith("[[") and value.endswith("]]"):
        return value
    return f"[[{value}]]"


def _format_wikilink_list(items: Sequence[str]) -> str:
    """``"[[A]], [[B]], [[C]]"`` — pusta lista → ``"_(brak)_"``."""

    if not items:
        return "_(brak)_"
    parts = [_wrap_wikilink(i) for i in items if isinstance(i, str) and i.strip()]
    if not parts:
        return "_(brak)_"
    return ", ".join(parts)
