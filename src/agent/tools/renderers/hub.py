"""``render_hub`` — renderer notatki typu hub (Faza 5 refaktoru).

**Semantyka notatki typu hub (AthleteStack):**

Hub to wezel tematyczny — notatka, ktora agreguje wiedze o jednym obszarze
systemu (np. ``Architektura_systemu``, ``Infrastruktura_integracji``). W
przeciwienstwie do MOC (ktory jest ``type: MOC`` i trzyma **liste linkow**),
hub niesie **tresc merytoryczna** z gestym graffem wikilinkow do konceptow,
decyzji, modulow.

Typowe sekcje (w kolejnosci, wymuszanej przez renderer):

1. **``overview``** — 2-5 zdan "o czym ten hub". Prolog bez tytulu.
2. **``{sections[].heading}``** — dowolna liczba custom sekcji od modelu
   (np. "Warstwy systemu", "Decyzje architektoniczne", "Kluczowe moduly").
   Kolejnosc: w kolejnosci w jakiej zostaly podane.
3. **Powiazane notatki** — stopka z bulletami ``[[X]] — opis``, gdy podane
   w ``related_notes`` args. Opcjonalna.

Renderer NIE dodaje od siebie sekcji "Dzieci" / "Sub-huby" — to robi
wlasciciel struktury (MOC manager post-hoc albo user). Hub nie musi
wiedziec "co pod nim zyje" — to nawigacja wsteczna (Obsidian backlinks).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.agent.tools.renderers._frontmatter import build_frontmatter

__all__ = ["HubSection", "HubRelatedEntry", "render_hub"]


@dataclass(slots=True, frozen=True)
class HubSection:
    """Pojedyncza sekcja ``##`` huba, skladana z czystych pol.

    :ivar heading: tytul sekcji (bez ``#``). Renderer doklei ``## ``.
    :ivar body: markdown body sekcji. Moze zawierac listy, tabele, bloki
        kodu — renderer **nie** modyfikuje tresci (poza normalizacja
        newline'ow wokol sekcji).
    """

    heading: str
    body: str


@dataclass(slots=True, frozen=True)
class HubRelatedEntry:
    """Wpis w stopce "Powiazane notatki".

    :ivar wikilink: ``"X"`` (zostanie owinieta ``[[X]]``) lub juz owiniety.
    :ivar description: krotki opis "dlaczego powiazane" — opcjonalny.
    """

    wikilink: str
    description: str | None = None


def render_hub(
    *,
    title: str,
    overview: str,
    sections: Sequence[HubSection],
    parent: str | None,
    related: Sequence[str] | None,
    tags: Sequence[str] | None,
    created: str,
    updated: str | None = None,
    status: str | None = None,
    related_notes: Sequence[HubRelatedEntry] | None = None,
) -> str:
    """Sklada pelny markdown notatki typu ``hub``.

    :param title: ``# {title}`` na poczatku body (po frontmatterze).
    :param overview: prolog 2-5 zdan. Bez wlasnego headingu — renderer
        wstawia go bezposrednio pod ``# title``.
    :param sections: lista sekcji ``##`` w kolejnosci jaka zobaczy user.
        Pusta lista = sam prolog (rzadki, ale dozwolony przypadek dla
        bardzo mlodego huba).
    :param parent: wikilink do rodzicielskiego MOC (``"MOC___Kompendium"``
        albo ``"[[MOC___Kompendium]]"``). Renderer normalizuje.
    :param related: lista wikilinkow do pola ``related`` we frontmatterze.
    :param tags: tagi. Tag ``hub`` dodany automatycznie (wymuszone przez
        ``build_frontmatter``).
    :param created, updated: daty YYYY-MM-DD. ``updated=None`` → kopia ``created``.
    :param status: patrz ``build_frontmatter``.
    :param related_notes: opcjonalna lista wpisow do stopki "## Powiazane
        notatki" z krotkimi opisami. ``None`` albo pusta → sekcja pominieta.
    """

    if not isinstance(title, str) or not title.strip():
        raise ValueError("title huba musi byc niepustym stringiem")
    if not isinstance(overview, str) or not overview.strip():
        raise ValueError("overview huba musi byc niepustym stringiem")

    fm = build_frontmatter(
        note_type="hub",
        tags=tags,
        parent=parent,
        related=related,
        status=status,
        created=created,
        updated=updated,
    )

    body_parts: list[str] = [f"# {title.strip()}", "", overview.strip(), ""]

    for section in sections:
        if not isinstance(section, HubSection):
            raise TypeError(
                f"sections musi byc Sequence[HubSection], dostalismy {type(section).__name__}"
            )
        heading = section.heading.strip()
        body = section.body.rstrip()
        if not heading:
            raise ValueError("HubSection.heading nie moze byc pusty")
        body_parts.append(f"## {heading}")
        body_parts.append("")
        if body:
            body_parts.append(body)
        body_parts.append("")

    if related_notes:
        body_parts.append("## Powiazane notatki")
        body_parts.append("")
        for entry in related_notes:
            if not isinstance(entry, HubRelatedEntry):
                raise TypeError(
                    "related_notes musi byc Sequence[HubRelatedEntry], dostalismy "
                    f"{type(entry).__name__}"
                )
            body_parts.append(_format_related_bullet(entry))
        body_parts.append("")

    body = "\n".join(body_parts).rstrip() + "\n"
    return fm + "\n" + body


def _format_related_bullet(entry: HubRelatedEntry) -> str:
    wikilink = entry.wikilink.strip()
    if not wikilink:
        raise ValueError("HubRelatedEntry.wikilink nie moze byc pusty")
    if not (wikilink.startswith("[[") and wikilink.endswith("]]")):
        wikilink = f"[[{wikilink}]]"
    desc = (entry.description or "").strip()
    if desc:
        return f"- {wikilink} — {desc}"
    return f"- {wikilink}"
