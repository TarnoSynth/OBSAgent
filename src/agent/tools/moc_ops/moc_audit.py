"""``moc_audit`` - raport luk w MOC-u: co trzeba stworzyc / dopiac.

**Rola:**

Bez tego narzedzia MOCAgent grawa wslepego - musialby wolac wielokrotnie
``list_notes`` / ``read_note`` / ``vault_map`` zeby zorientowac sie co jest
w vaulcie i czego brakuje w MOC. ``moc_audit`` robi to w **jednej turze**
i daje LLM ustrukturyzowany raport:

- jakie typy notatek sa w vaulcie + ile ich;
- ktore moduly lecą **bezposrednio** do MOC (kandydaci do pogrupowania przez hub);
- sugestie hubow: heurystyka po prefiksie (``Agent_*``, ``Vault_*``, ``Git_*``, ...);
- stan MOC: czy istnieje, ile ma sekcji, ile linkow w kazdej, czy sa puste sekcje;
- orphan wikilinks w MOC (linki ktore nie rozwiazuja sie na zadna notatke -
  czerwony alert);
- lista istniejacych hubow / technologies / concepts (zeby LLM nie dublowal).

**Bezpieczenstwo:**

Narzedzie jest **read-only** - nic nie zapisuje, nic nie proponuje do
zapisania. Jedynie zwraca strukture + krotki markdown dla LLM-a.

**Format wyjscia:**

``ToolResult.structured`` niesie pelny dict (przydatne do logowania, i LLM
dostaje go serializowanego w ``content``). ``ToolResult.content`` to krotki
markdown, ktory LLM czyta szybko i podejmuje decyzje co z czym zrobic.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext
from src.vault.moc import (
    MOC_SECTION_TITLES,
    moc_section_for_type,
)


class _MocAuditArgs(BaseModel):
    """Argumenty ``moc_audit``."""

    model_config = ConfigDict(extra="forbid")

    moc_path: str = Field(
        "MOC___Kompendium.md",
        min_length=1,
        description=(
            "Sciezka relatywna do pliku MOC (np. 'MOC___Kompendium.md'). "
            "Default = glowny MOC vaulta. Moze nie istniec — audit wtedy "
            "zwroci 'moc_exists: false' i agent wie ze trzeba go stworzyc."
        ),
    )
    language: str = Field(
        "pl",
        description=(
            "Jezyk tytulow sekcji - 'pl' (Moduly/Huby/Technologie/...) albo 'en' "
            "(Modules/Hubs/...). Uzywane do rozpoznawania ktore sekcje sa puste."
        ),
    )
    hub_prefix_min_count: int = Field(
        3,
        ge=2,
        le=20,
        description=(
            "Minimalna liczba modulow z tym samym prefiksem (np. 'Agent_') zeby "
            "zaproponowac stworzenie huba. Default = 3 (2 to za malo zeby "
            "uzasadnic osobny hub). Zwiekszaj jesli chcesz tylko duze grupy."
        ),
    )


#: Wzorzec wyciagajacy prefix z nazwy stem modulu: ``Agent_ActionExecutor`` ->
#: ``Agent``, ``Git_Reader`` -> ``Git``, ``Vault_Manager`` -> ``Vault``.
#: Single-word stem (np. ``Agent``) daje prefix = cala nazwa - wiec nie
#: sugerujemy huba dla pojedynczych modulow.
_PREFIX_RE = re.compile(r"^([A-Z][a-zA-Z0-9]+?)(?:_|$)")

#: Wzorzec wikilinku w tresci MOC (w body, poza frontmatterem): ``[[Stem]]``
#: ewentualnie z aliasem ``[[Stem|alias]]`` albo sekcja ``[[Stem#h]]``.
_WIKILINK_IN_BODY_RE = re.compile(r"\[\[([^\]]+?)\]\]")

#: Heading na poziomie H2 (``## Tytul``) - uzywane do liczenia sekcji MOC.
_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

#: Placeholder bootstrap MOC - ``_(pusto — ...)_`` / ``_(empty — ...)_``.
_PLACEHOLDER_RE = re.compile(r"^_\(\s*(pusto|empty)\b[^)]*\)_\s*$", re.MULTILINE)


class MocAuditTool(Tool):
    """Raport luk w MOC: typy notatek, sugestie hubow, puste sekcje, orphany."""

    name = "moc_audit"
    description = (
        "Zwraca ustrukturyzowany raport stanu MOC-u i vaulta: statystyki per "
        "typ notatki, moduly bez huba (kandydaci do grupowania), sugestie "
        "hubow na bazie prefiksu nazwy, liste istniejacych hubow/technologii/"
        "konceptow (zeby nie dublowac), stan sekcji MOC (puste/wypelnione), "
        "orphan wikilinks w MOC. Read-only, niczego nie zapisuje. **Woluj "
        "jako PIERWSZE narzedzie w sesji MOC** - bez tego nie wiesz co robic."
    )

    def input_schema(self) -> dict[str, Any]:
        return _MocAuditArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _MocAuditArgs.model_validate(args)
        except Exception as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        knowledge = ctx.ensure_vault_knowledge()
        moc_stem = Path(parsed.moc_path).stem
        language = parsed.language if parsed.language in MOC_SECTION_TITLES else "pl"

        type_counts: Counter[str] = Counter()
        for note in knowledge.notes:
            type_counts[note.type or "<none>"] += 1

        hubs = sorted(
            Path(p).stem for p in knowledge.by_type.get("hub", [])
        )
        technologies = sorted(
            Path(p).stem for p in knowledge.by_type.get("technology", [])
        )
        concepts = sorted(
            Path(p).stem for p in knowledge.by_type.get("concept", [])
        )
        decisions = sorted(
            Path(p).stem for p in knowledge.by_type.get("decision", [])
            + knowledge.by_type.get("adr", [])
        )

        modules_without_hub: list[dict[str, str]] = []
        prefix_groups: dict[str, list[str]] = defaultdict(list)
        for note_path in knowledge.by_type.get("module", []):
            note = knowledge.by_path.get(note_path)
            if note is None:
                continue
            stem = Path(note_path).stem
            parent_stem = note.parent
            if parent_stem is None or parent_stem == moc_stem:
                modules_without_hub.append({"stem": stem, "parent": parent_stem or ""})
            m = _PREFIX_RE.match(stem)
            if m:
                prefix = m.group(1)
                if prefix != stem:
                    prefix_groups[prefix].append(stem)

        hub_suggestions: list[dict[str, Any]] = []
        existing_hub_prefixes = {h.lower() for h in hubs}
        for prefix, members in sorted(prefix_groups.items()):
            if len(members) < parsed.hub_prefix_min_count:
                continue
            if prefix.lower() in existing_hub_prefixes:
                continue
            hub_suggestions.append(
                {
                    "prefix": prefix,
                    "member_count": len(members),
                    "members": sorted(members),
                    "suggested_stem": f"Hub_{prefix}",
                }
            )

        moc_note = knowledge.by_path.get(parsed.moc_path)
        moc_exists = moc_note is not None
        moc_sections: list[dict[str, Any]] = []
        orphan_wikilinks_in_moc: list[str] = []
        moc_content = ""

        if moc_note is not None:
            moc_content = moc_note.content
            h2_positions = [
                (m.start(), m.group(1).strip()) for m in _H2_RE.finditer(moc_content)
            ]
            for i, (start, title) in enumerate(h2_positions):
                end = h2_positions[i + 1][0] if i + 1 < len(h2_positions) else len(moc_content)
                section_body = moc_content[start:end]
                link_matches = list(_WIKILINK_IN_BODY_RE.finditer(section_body))
                link_count = len(link_matches)
                has_placeholder = bool(_PLACEHOLDER_RE.search(section_body))
                # heurystyka "ma cos wiecej niz link lista i placeholder":
                # liczymy linie ktore nie sa: naglowkiem, pusta, link-bullet
                has_prose = False
                for line in section_body.splitlines():
                    s = line.strip()
                    if not s:
                        continue
                    if s.startswith("##"):
                        continue
                    if s.startswith("- [[") and s.endswith("]]"):
                        continue
                    if _PLACEHOLDER_RE.match(s):
                        continue
                    if s.startswith("|"):  # tabela np. decyzji
                        continue
                    has_prose = True
                    break
                moc_sections.append(
                    {
                        "title": title,
                        "link_count": link_count,
                        "has_placeholder": has_placeholder,
                        "has_prose": has_prose,
                    }
                )
                for lm in link_matches:
                    target = lm.group(1).split("|", 1)[0].split("#", 1)[0].strip()
                    if not target or target == moc_stem:
                        continue
                    if knowledge.resolve(target) is None:
                        orphan_wikilinks_in_moc.append(target)

        # Statystyka linkow z modulow - fundament dla decyzji LLM o technologiach
        # i konceptach. Deterministycznie nie da sie odroznic "technologii" od
        # "konceptu" od "innego modulu" po samej nazwie stemu, ale mozemy dac
        # LLM-owi pelen obraz: dla kazdego unikalnego targeta wikilinka w
        # modulach liczymy liczbe *modulow* ktore go wspominaja + flagi:
        #   - resolves_to: typ notatki jesli istnieje, None jesli orphan
        #   - source_modules: lista modulow (dla samplingu przez read_note)
        #
        # LLM po tym wie "ten target pojawia sie w 8 modulach, jest orphanem -
        # to mocny kandydat do stworzenia notatki; typ zdecyduje po przeczytaniu
        # 1-2 modulow zrodlowych". Zero deterministycznej klasyfikacji tu.
        # UWAGA: source_modules to **pelne sciezki** (np. "Module___Agent_Models.md"),
        # nie stemy. ``read_note(path=...)`` wymaga sciezek — jesli damy tu stem
        # ("Agent_Models"), LLM skopiuje go jak leci i wszystkie read_note padna
        # jako "note not found". Najtansza poprawka klasy bledu: serializujemy
        # to co faktycznie mozna wkleic do read_note bez translacji.
        link_freq: dict[str, list[str]] = defaultdict(list)
        for note_path in knowledge.by_type.get("module", []):
            note = knowledge.by_path.get(note_path)
            if note is None:
                continue
            seen_in_this_module: set[str] = set()
            for link in note.wikilinks:
                target = link.split("|", 1)[0].split("#", 1)[0].strip()
                if not target or target in seen_in_this_module:
                    continue
                seen_in_this_module.add(target)
                link_freq[target].append(note_path)

        link_targets_from_modules: list[dict[str, Any]] = []
        for target, sources in sorted(
            link_freq.items(), key=lambda kv: (-len(kv[1]), kv[0])
        ):
            resolved = knowledge.resolve(target)
            link_targets_from_modules.append(
                {
                    "target": target,
                    "mention_count": len(sources),
                    "resolves_to_type": resolved.type if resolved else None,
                    "source_modules": sorted(sources)[:8],
                }
            )

        orphan_candidates_from_modules = sorted(
            {
                t["target"]
                for t in link_targets_from_modules
                if t["resolves_to_type"] is None
            }
        )

        expected_sections = sorted(MOC_SECTION_TITLES.get(language, {}).values())
        existing_section_titles = {s["title"] for s in moc_sections}
        missing_sections = [s for s in expected_sections if s not in existing_section_titles]

        structured: dict[str, Any] = {
            "moc_path": parsed.moc_path,
            "moc_exists": moc_exists,
            "language": language,
            "vault_totals": {
                "total_notes": knowledge.total_notes,
                "by_type": dict(type_counts),
            },
            "moc_sections": moc_sections,
            "missing_sections_for_language": missing_sections,
            "hubs_existing": hubs,
            "technologies_existing": technologies,
            "concepts_existing": concepts,
            "decisions_existing": decisions,
            "modules_without_hub": modules_without_hub,
            "hub_suggestions": hub_suggestions,
            "orphan_wikilinks_in_moc": sorted(set(orphan_wikilinks_in_moc)),
            "orphan_wikilinks_in_modules": orphan_candidates_from_modules,
            "link_targets_from_modules": link_targets_from_modules,
        }

        # Krotki markdown dla LLM-a - kluczowe sygnaly na gorze, zeby model
        # nie musial mielic calego JSON-a (ten jest w structured + serialize).
        lines: list[str] = []
        lines.append(f"# MOC audit: {parsed.moc_path}")
        lines.append("")
        if not moc_exists:
            lines.append("**MOC nie istnieje** - stworz go przez create_note typu 'moc'.")
            lines.append("")
        lines.append(f"Vault: {knowledge.total_notes} notatek.")
        lines.append(
            "Rozklad po type: "
            + ", ".join(f"{t}={n}" for t, n in sorted(type_counts.items()))
        )
        lines.append("")
        lines.append(
            "> **UWAGA:** Ten audit to **mapa sygnalow**, nie prawda absolutna. "
            "Sekcje `technologies_existing` / `concepts_existing` zliczaja tylko "
            "notatki z frontmatter `type: technology` / `type: concept`. Realne "
            "technologie i koncepty uzywane w projekcie **nie sa z gory w zadnym "
            "deterministycznym zrodle** - musisz je wylowic sam, czytajac tresc "
            "modulow (`read_note`). Sekcja **Linki z modulow** ponizej to Twoj "
            "punkt startowy."
        )
        lines.append("")

        if moc_exists and moc_sections:
            lines.append("## Sekcje MOC")
            for s in moc_sections:
                mark = []
                if s["link_count"] == 0 and not s["has_prose"]:
                    mark.append("PUSTA")
                if s["has_placeholder"]:
                    mark.append("placeholder")
                if s["has_prose"]:
                    mark.append("prose")
                tag = f" [{', '.join(mark)}]" if mark else ""
                lines.append(f"- {s['title']}: {s['link_count']} linkow{tag}")
            lines.append("")

        if missing_sections:
            lines.append(
                "## Brakujace sekcje (wg konwencji " + language + ")"
            )
            for title in missing_sections:
                lines.append(f"- {title}")
            lines.append("")

        if hub_suggestions:
            lines.append("## Sugestie hubow (grupy modulow po prefiksie)")
            for s in hub_suggestions:
                lines.append(
                    f"- **{s['suggested_stem']}** ({s['member_count']} modulow): "
                    + ", ".join(f"[[{m}]]" for m in s["members"][:10])
                    + ("..." if len(s["members"]) > 10 else "")
                )
            lines.append("")

        if modules_without_hub:
            lines.append(
                f"## Moduly bez huba ({len(modules_without_hub)}) - lecą bezposrednio do MOC"
            )
            preview = modules_without_hub[:15]
            for m in preview:
                lines.append(f"- [[{m['stem']}]] (parent={m['parent'] or '<brak>'})")
            if len(modules_without_hub) > 15:
                lines.append(f"- ... +{len(modules_without_hub) - 15} wiecej")
            lines.append("")

        if hubs:
            lines.append(f"## Istniejace huby ({len(hubs)})")
            lines.append(", ".join(f"[[{h}]]" for h in hubs))
            lines.append("")
        if technologies:
            lines.append(f"## Istniejace technologie ({len(technologies)})")
            lines.append(", ".join(f"[[{t}]]" for t in technologies))
            lines.append("")
        if concepts:
            lines.append(f"## Istniejace koncepty ({len(concepts)})")
            lines.append(", ".join(f"[[{c}]]" for c in concepts))
            lines.append("")
        if decisions:
            lines.append(f"## Istniejace decyzje ({len(decisions)})")
            lines.append(", ".join(f"[[{d}]]" for d in decisions))
            lines.append("")

        if structured["orphan_wikilinks_in_moc"]:
            lines.append(
                f"## ALERT: orphan wikilinks w MOC ({len(structured['orphan_wikilinks_in_moc'])})"
            )
            lines.append("Wskazuja na notatki ktore NIE istnieja w vaulcie - do usuniecia albo stworzenia:")
            for t in structured["orphan_wikilinks_in_moc"][:20]:
                lines.append(f"- [[{t}]]")
            lines.append("")

        # Rdzen nawigacyjny: top wikilink targetow z modulow. Pokazujemy TOP-30
        # posortowane po mention_count desc. Kolumna "resolves" informuje czy
        # target ma juz notatke (i jakiego typu) czy jest orphanem - LLM ma
        # ustawione priorytety bez dalszego mielenia JSON-a.
        if link_targets_from_modules:
            lines.append(
                f"## Linki z modulow ({len(link_targets_from_modules)} unikalnych, top 30)"
            )
            lines.append(
                "Kazdy wiersz: `[[Target]] xN -> typ-jesli-istnieje | read_note: <sciezki>`. "
                "Orphans (brak typu) to kandydaci do stworzenia - ale **nie zgadujesz typu** "
                "z samej nazwy, tylko **read_note** na 1-2 modulach zrodlowych, zeby "
                "zrozumiec co to faktycznie jest. **Skopiuj ścieżkę z `read_note:` 1:1** "
                "(razem z rozszerzeniem `.md`) - nie przerabiaj na stemy."
            )
            for entry in link_targets_from_modules[:30]:
                target = entry["target"]
                cnt = entry["mention_count"]
                typ = entry["resolves_to_type"]
                srcs = entry["source_modules"]
                if typ is None:
                    marker = "**ORPHAN**"
                else:
                    marker = f"-> {typ}"
                # pokazujemy pierwsze 2 PELNE sciezki gotowe do wklejenia
                # do read_note(path="..."). Reszte w +N bez sciezki zeby nie
                # puchla linia.
                if srcs:
                    sample = " | ".join(f"`{s}`" for s in srcs[:2])
                    more = f" (+{len(srcs) - 2})" if len(srcs) > 2 else ""
                else:
                    sample = "(brak)"
                    more = ""
                lines.append(
                    f"- [[{target}]] x{cnt} {marker} - read_note: {sample}{more}"
                )
            if len(link_targets_from_modules) > 30:
                lines.append(
                    f"- ... +{len(link_targets_from_modules) - 30} wiecej (patrz structured)"
                )
            lines.append("")

        if orphan_candidates_from_modules:
            lines.append(
                f"## Sam orphan list ({len(orphan_candidates_from_modules)})"
            )
            lines.append(
                "Podzbior powyzszego - tylko targety ktore NIE maja notatki. "
                "**Nie zgaduj** czy to technologia, koncept czy moduł - przeczytaj "
                "jeden z modulow zrodlowych przez `read_note`."
            )
            preview = orphan_candidates_from_modules[:20]
            lines.append(", ".join(f"[[{t}]]" for t in preview))
            if len(orphan_candidates_from_modules) > 20:
                lines.append(f"... +{len(orphan_candidates_from_modules) - 20} wiecej")
            lines.append("")

        lines.append("---")
        lines.append("**Dalsze kroki (algorytm):**")
        lines.append(
            "1. Przejrzyj **Linki z modulow** powyzej - zobacz co moduly realnie "
            "linkuja. Wybierz 3-7 top-mention orphans jako kandydatow."
        )
        lines.append(
            "2. Dla **kazdego** kandydata: `read_note` na 1-2 modulach zrodlowych "
            "(`source_modules`). Zdecyduj czy to technologia, koncept, inny modul, "
            "czy sie zignoruje (szum)."
        )
        lines.append(
            "3. Dopiero teraz `create_technology` / `create_concept` / `create_hub` - "
            "kazda notatka z **konkretnym opisem** bazowanym na tym co przeczytales "
            "w modulach, nie na zgadywaniu."
        )
        lines.append(
            "4. `add_moc_link` dopina do odpowiedniej sekcji MOC. `moc_set_intro` "
            "domyka wprowadzenie. `submit_plan` zamyka sesje."
        )

        content = "\n".join(lines)
        ctx.record_action(
            tool=self.name,
            path=parsed.moc_path,
            args={"language": language, "hub_prefix_min_count": parsed.hub_prefix_min_count},
            ok=True,
        )
        return ToolResult(ok=True, content=content, structured=structured)


__all__ = ["MocAuditTool"]
