"""``create_hub`` — tworzy nowa notatke typu hub (Faza 5 refaktoru).

**Semantyka:**

Rejestruje ``ProposedWrite(type="create", ...)`` dla nowego pliku typu ``hub``.
Model przekazuje **strukturowane pola** (``title``, ``overview``,
``sections[]``, ``parent_moc``, ``related``, ``tags``), renderer (w
``src.agent.tools.renderers.hub``) sklada z nich konsystentny markdown z
frontmatterem. W ten sposob schemat narzedzia **uczy model** typologii
AthleteStack — zamiast pisac gole markdowny, model wypelnia pola.

**Preconditions (jak ``create_note``):**

- ``path`` walidowalna (``.md``, relatywna, bez ``..``)
- ``path`` nie istnieje w vaulcie ani jako pending create w tej sesji
- ``parent_moc`` pasuje do wzorca MOC w configu (``MOC___{name}``) —
  ostrzezenie gdy ``MOC__`` (legacy). Walidacja jest **wyrozniona**:
  niewazny MOC → ``ok=False`` (model ma poprawic), bo hub bez rodzicielskiego
  MOC-a to konceptualny blad.

Blad walidacji Pydantic → ``ToolResult(ok=False, error=...)`` zeby model
mogl sie poprawic bez rzucania wyjatku.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext
from src.agent.tools.renderers.hub import (
    HubRelatedEntry,
    HubSection,
    render_hub,
)
from src.agent.tools.vault_write._common import (
    build_and_register_action,
    normalize_path_or_error,
    path_exists_effectively,
)

__all__ = ["CreateHubTool"]


class _HubSectionArg(BaseModel):
    """Pole ``sections[]`` — sekcja ## huba.

    Model uczy sie, ze hub ma **wiele sekcji**. Minimalna sekcja huba
    to "Przeglad", "Wezly", "Decyzje". Renderer zachowuje kolejnosc.
    """

    model_config = ConfigDict(extra="forbid")

    heading: str = Field(
        ...,
        min_length=1,
        description=(
            "Tytul sekcji ``##`` (bez ``#``). Krotki, rzeczownik albo fraza. "
            "Np. 'Warstwy systemu', 'Decyzje architektoniczne', 'Kluczowe moduly'."
        ),
    )
    body: str = Field(
        ...,
        min_length=1,
        description=(
            "Markdown body sekcji — mozesz uzywac list, tabel, blokow kodu, "
            "wikilinkow ``[[X]]``. Nie zagniezdzaj headingow wyzszych niz "
            "``###`` (``##`` jest zarezerwowany dla kolejnej sekcji huba)."
        ),
    )


class _HubRelatedArg(BaseModel):
    """Pole ``related_notes[]`` — wpis w stopce 'Powiazane notatki'."""

    model_config = ConfigDict(extra="forbid")

    wikilink: str = Field(
        ...,
        min_length=1,
        description=(
            "Nazwa notatki do zlinkowania (np. ``Qdrant`` lub ``[[Qdrant]]``). "
            "Renderer sam owinie w ``[[...]]`` jesli potrzeba."
        ),
    )
    description: str | None = Field(
        default=None,
        description="Krotki opis 'dlaczego powiazane' (1 zdanie). Opcjonalne.",
    )


class _CreateHubArgs(BaseModel):
    """Schemat argumentow narzedzia ``create_hub`` — zrodlo prawdy dla ``input_schema``.

    Pola ``parent_moc``, ``related``, ``tags`` sa normalizowane przez
    renderer (patrz ``build_frontmatter``) — model moze przekazac ``"MOC___X"``
    albo ``"[[MOC___X]]"``, oba formy zadzialaja.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ...,
        min_length=1,
        description=(
            "Sciezka relatywna do vaulta, ``.md``. Np. 'hubs/Architektura_systemu.md'. "
            "Bez ``..``, bez prefiksu ``/``."
        ),
    )
    title: str = Field(
        ...,
        min_length=1,
        description="Tytul huba — trafi do ``# title`` po frontmatterze.",
    )
    overview: str = Field(
        ...,
        min_length=1,
        description=(
            "2-5 zdan prologu pod tytulem: o czym ten hub, gdzie zyje w grafie "
            "wiedzy, dla kogo jest. Bez headingu (renderer wstawia body pod "
            "``# title`` bezposrednio)."
        ),
    )
    sections: list[_HubSectionArg] = Field(
        ...,
        min_length=1,
        description=(
            "Lista sekcji ``##`` w kolejnosci jak maja sie pokazac w notatce. "
            "Kazda sekcja ma tytul + markdown body. Typowo 3-6 sekcji."
        ),
    )
    parent_moc: str = Field(
        ...,
        min_length=1,
        description=(
            "Wikilink do rodzicielskiego MOC-a (np. ``MOC___Kompendium`` albo "
            "``[[MOC___Kompendium]]``). KAZDY hub MUSI miec parent MOC — hub "
            "bez MOC-a jest orphanem w grafie wiedzy."
        ),
    )
    related: list[str] | None = Field(
        default=None,
        description=(
            "Lista wikilinkow do pola ``related`` we frontmatterze. Mozna pusta. "
            "Powiazania koncepcyjne — np. hub 'Architektura' related do hubu 'Infrastruktura'."
        ),
    )
    tags: list[str] | None = Field(
        default=None,
        description=(
            "Tagi dodatkowe (poza automatycznym ``hub``). Np. ``['core', 'architecture']``. "
            "Bez ``#``. Renderer sam dodaje tag ``hub``."
        ),
    )
    created: str = Field(
        ...,
        min_length=10,
        max_length=10,
        description="Data w formacie ``YYYY-MM-DD`` — data commita projektowego.",
    )
    updated: str | None = Field(
        default=None,
        description="Data ``YYYY-MM-DD``. ``None`` = kopia ``created``.",
    )
    status: str | None = Field(
        default=None,
        description="``active`` / ``draft`` / ``archived``. Domyslnie ``active``.",
    )
    related_notes: list[_HubRelatedArg] | None = Field(
        default=None,
        description=(
            "Opcjonalna lista wpisow do sekcji '## Powiazane notatki' w body. "
            "Rozne od pola ``related`` (ktore idzie do frontmattera) — ta lista "
            "to stopka widoczna przez usera w Obsidianie."
        ),
    )


def _validate_parent_moc(parent_moc: str) -> str | ToolResult:
    """Sprawdza, ze ``parent_moc`` wskazuje na plik wygladajacy na MOC.

    Akceptuje ``MOC___X`` (primary) i ``MOC__X`` (legacy, z logiem w
    ``MOCManager``). Odrzuca ``X`` bez prefiksu MOC — hub MUSI miec MOC-a
    jako parent, nie zwykla notatke.

    Zwraca znormalizowany wikilink ``[[MOC___X]]`` albo ``ToolResult(ok=False)``.
    """

    value = parent_moc.strip()
    if value.startswith("[[") and value.endswith("]]"):
        inner = value[2:-2].strip()
    else:
        inner = value
    stem = inner.split("|", 1)[0].strip()
    if not stem:
        return ToolResult(
            ok=False,
            error="parent_moc nie moze byc pusty",
        )
    if not (stem.startswith("MOC___") or stem.startswith("MOC__")):
        return ToolResult(
            ok=False,
            error=(
                f"parent_moc {parent_moc!r} nie wyglada jak MOC "
                "(oczekiwany prefiks 'MOC___' lub legacy 'MOC__'). Hub MUSI "
                "miec MOC jako parent — nie zwykla notatke."
            ),
        )
    return f"[[{stem}]]"


class CreateHubTool(Tool):
    """Tworzy nowa notatke typu ``hub`` w stylu AthleteStack."""

    name = "create_hub"
    description = (
        "Tworzy hub (wezel tematyczny typu ``hub``) — notatke agregujaca wiedze "
        "o jednym obszarze (np. 'Architektura systemu'). Wymaga rodzicielskiego "
        "MOC (``parent_moc``). Renderer sklada markdown deterministycznie ze "
        "strukturowanych pol (overview, sections[], related_notes[]). "
        "Nic nie zapisuje natychmiast — finalizacja przez submit_plan."
    )

    def input_schema(self) -> dict[str, Any]:
        return _CreateHubArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _CreateHubArgs.model_validate(args)
        except ValidationError as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        normalized_path = normalize_path_or_error(parsed.path)
        if isinstance(normalized_path, ToolResult):
            return normalized_path

        if path_exists_effectively(ctx, normalized_path):
            return ToolResult(
                ok=False,
                error=(
                    f"path exists: {normalized_path!r} — hub o tej sciezce juz istnieje. "
                    "Uzyj 'update_note' lub 'append_section' zamiast 'create_hub'."
                ),
            )

        parent_moc_result = _validate_parent_moc(parsed.parent_moc)
        if isinstance(parent_moc_result, ToolResult):
            return parent_moc_result
        parent_moc_wikilink = parent_moc_result

        sections = [
            HubSection(heading=s.heading, body=s.body) for s in parsed.sections
        ]
        related_notes = None
        if parsed.related_notes:
            related_notes = [
                HubRelatedEntry(wikilink=rn.wikilink, description=rn.description)
                for rn in parsed.related_notes
            ]

        content = render_hub(
            title=parsed.title,
            overview=parsed.overview,
            sections=sections,
            parent=parent_moc_wikilink,
            related=_as_sequence(parsed.related),
            tags=_as_sequence(parsed.tags),
            created=parsed.created,
            updated=parsed.updated,
            status=parsed.status,
            related_notes=related_notes,
        )

        result = build_and_register_action(
            ctx=ctx,
            tool_name=self.name,
            action_type="create",
            normalized_path=normalized_path,
            content=content,
        )
        if result.ok:
            stem = Path(normalized_path).stem
            result = ToolResult(
                ok=True,
                content=(
                    f"{result.content}\n"
                    f"HUB created: stem='{stem}', parent={parent_moc_wikilink}, "
                    f"sections={len(parsed.sections)}."
                ),
            )
        return result


def _as_sequence(value: Sequence[str] | None) -> Sequence[str] | None:
    return value if value else None
