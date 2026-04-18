"""Budowanie prompt\u00f3w user-level dla trzech tryb\u00f3w pracy agenta.

Tryby biegu (decyzja user: `delivery=summarize_first`):

1. **Small commit** \u2014 caly diff miesci sie w jednym chunku.
   - Jeden request: system_prompt + ``build_user_prompt`` + tool submit_plan.
   - Stary swinski flow, teraz bazuje na ``ChunkedCommit`` zamiast ``CommitInfo``.

2. **Chunk summary** (multi-turn step 1..N) \u2014 jeden chunk -> krotkie
   podsumowanie od AI, bez tool calls.
   - ``build_chunk_summary_prompt`` zwraca tylko tresc chunka + meta
     (ktory plik, ktory chunk, context commita). System prompt jest
     osobny (``load_chunk_instruction_prompt``).

3. **Finalize** (multi-turn step N+1) \u2014 zebrane podsumowania lecia do AI
   z wszystkim kontekstem vaulta + tool submit_plan.
   - ``build_finalize_prompt`` zamiast pelnych diffow wstawia
     zgromadzone ``ChunkSummary`` pogrupowane per plik.

Wszystkie buildery zwracaja stringi gotowe do ``MessageRole.USER``.
System prompty wczytujemy osobno w ``Agent`` (z ``prompts.py``).

Podzial sekcji w ``build_user_prompt`` i ``build_finalize_prompt`` jest
**swiadomie** redundantny \u2014 mozna by wydzielic wspolne helpery, ale oba
flow-y sa sobie bliskie a nie identyczne i mieszanie ich przez If-y
robi kod trudniejszym do czytania. Duplikacja jest tania, nieczytelnosc
droga.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.agent.models_chunks import ChunkedCommit, ChunkSummary, DiffChunk
from src.git.models import CommitInfo
from src.vault.models import VaultKnowledge, VaultNote


_MAX_VAULT_KNOWLEDGE_NOTES = 250
"""Gorny limit notatek w `VaultKnowledge` dopisywanych do prompta.

**Uwaga (Faza 4 refaktoru):** od Fazy 4 sekcja "mapa wiedzy" jest
**mocno odchudzona** — zwraca tylko liczniki per typ + liste MOC-ow/hubow.
Pelna lista wszystkich notatek zniknela z promptu; model pobiera szczegoly
on-demand przez narzedzia ``list_notes`` / ``read_note`` / ``find_related``.

Stala zachowana dla backward-compat i dla trybu debug (gdyby user chcial
zrzucic pelna liste, mozemy wskrzesic starsza sciezke pod flaga)."""

_MAX_HUB_LIST = 40
"""Limit wpisow na liscie hubow (``type: hub``) w prompcie.

Vault AthleteStack-style ma 10-30 hubow. Powyzej 40 lista staje sie
nieprzydatna — agent i tak powinien szukac po filtrach, nie skanowac
wizualnie."""

_MAX_TOP_TAGS = 15
"""Ile najpopularniejszych tagow listujemy w mapie top-level.

Dajemy modelowi landscape tagow zamiast samego licznika — dzieki temu
pierwsze wywolanie ``list_notes``/``list_tags`` moze od razu zawezic
przez ``tag=`` zamiast dumpowac caly vault. 15 tagow ~200 tokenow,
laczy sie z prompt cachingiem (stabilny prefiks miedzy commitami)."""

_MAX_TYPE_EXAMPLES = 5
"""Ile przykladowych stemow dopisujemy do kazdego typu w mapie top-level.

Np. ``module (15): Auth, Billing, Checkout, Notifications, Payments
(i 10 wiecej)``. Model od razu widzi "Auth juz istnieje jako modul" i
nie dubluje. Wiecej niz 5 stemow = szum, model zacznie je kopiowac
zamiast wolac ``list_notes``."""


def build_user_prompt(
    *,
    chunked_commit: ChunkedCommit,
    vault_changes: list[CommitInfo],
    vault_changed_notes: list[VaultNote],
    vault_knowledge: VaultKnowledge,
    templates: dict[str, str],
    project_name: str,
    retry_error: str | None = None,
    previous_actions: list[dict[str, Any]] | None = None,
) -> str:
    """User prompt dla trybu SMALL (caly diff mieszczacy sie w 1 chunku).

    Zaklada ``chunked_commit.total_chunks == 1``. Gdyby chunkow bylo
    wiecej, agent wybiera sciezke multi-turn i wola ``build_chunk_summary_prompt``
    oraz ``build_finalize_prompt`` zamiast tego budera.

    :param chunked_commit: commit gotowy po ``GitContextBuilder.prepare_commit``
    :param vault_changes: lista recznych commitow usera w vaulcie
    :param vault_changed_notes: aktualne tresci notatek zmienionych recznie
    :param vault_knowledge: aktualny stan vaulta (po ``scan_all``)
    :param templates: ``{"changelog": "...", "adr": "...", ...}``
    :param project_name: nazwa projektu (do kontekstu)
    :param retry_error: jesli retry \u2014 opis bledu walidacji poprzedniej odpowiedzi
    :param previous_actions: jesli retry \u2014 snapshot ``ctx.executed_actions``
        z poprzedniej proby. Renderowany jako lista "juz probowalem tego",
        zeby model nie zaczynal od eksploracji od zera.
    """

    sections: list[str] = []

    sections.append(_section_retry_error(retry_error))
    sections.append(_section_previous_actions(previous_actions))
    sections.append(_section_project_context(project_name))
    sections.append(_section_commit_with_chunks(chunked_commit))
    sections.append(_section_vault_changes(vault_changes, vault_changed_notes))
    sections.append(_section_vault_knowledge(vault_knowledge))
    sections.append(_section_templates(templates))
    sections.append(_section_task_small())

    return "\n\n".join(s for s in sections if s.strip())


def build_chunk_summary_prompt(
    *,
    chunked_commit: ChunkedCommit,
    chunk: DiffChunk,
    chunk_position: tuple[int, int],
    project_name: str,
) -> str:
    """User prompt dla pojedynczego chunk-summary (tryb multi-turn).

    Kontrakt: AI ma zwrocic 3-6 zdan zwyklego tekstu o tym, co widzi
    w tym chunku. Zadnych tool calls. Szczegoly "jak odpowiadac" siedza
    w system prompcie (``load_chunk_instruction_prompt``).

    :param chunked_commit: pelny ``ChunkedCommit`` \u2014 potrzebny zeby
        podac AI meta commita (sha, message, autor, data, statystyki).
        Inne chunki sa w nim obecne, ale ich NIE pokazujemy tu (AI widzi
        po kolei tylko jeden).
    :param chunk: chunk do zanalizowania w tej turze
    :param chunk_position: ``(globalny_idx, wszystkie)``, czyli "teraz
        analizujesz chunk 3 z 7 wszystkich w calym commicie". Pomaga AI
        kalibrowac zwiezlosc (pierwsze chunki: obszerniej, pozniejsze:
        krocej jesli sa podobne).
    :param project_name: nazwa projektu (krotka)
    """

    global_idx, global_total = chunk_position

    sections: list[str] = []

    sections.append(f"## Kontekst projektu\n\nProjekt: **{project_name}**.")
    sections.append(_section_commit_meta(chunked_commit.commit))

    files_line = ", ".join(f"`{p}`" for p in chunk.file_paths) or "_(brak)_"
    meta_lines = [
        f"- **ID chunka:** `{chunk.chunk_id}`",
        f"- **Pliki w tym chunku:** {files_line}",
        f"- **Typ chunka:** `{chunk.kind}`",
        f"- **Liczba hunkow:** {chunk.hunk_count}",
        f"- **Linii:** {chunk.line_count}",
    ]
    if chunk.is_split:
        meta_lines.append(
            f"- **Kontynuacja hunka:** czesc **{chunk.split_part}/{chunk.split_total}** "
            f"tej samej logicznej zmiany (split_group=`{chunk.split_group}`). "
            f"Inne chunki z tym samym `split_group` to kawalki dokladnie tego "
            f"samego hunka pliku \u2014 potraktuj je lacznie."
        )
    sections.append(
        f"## Fragment do analizy (chunk {global_idx}/{global_total} calego commita)\n\n"
        + "\n".join(meta_lines)
    )
    sections.append(
        "### Tresc chunka (diff)\n\n"
        f"```diff\n{chunk.diff_text.rstrip()}\n```"
    )
    sections.append(
        "## Zadanie dla tej tury\n\n"
        "Zwroc krotkie podsumowanie (3-6 zdan, zwykly tekst) tego fragmentu. "
        "Opisz co zmienione (w tym ktorych plikow dotyczy), jaka jest intencja, "
        "jakie elementy kodu widac. **Nie** wywoluj zadnego narzedzia. "
        "**Nie** proponuj akcji na vaulcie \u2014 to zrobi osobny, finalny "
        "prompt po zebraniu wszystkich podsumowan."
    )

    return "\n\n".join(s for s in sections if s.strip())


def build_finalize_prompt(
    *,
    chunked_commit: ChunkedCommit,
    chunk_summaries: list[ChunkSummary],
    vault_changes: list[CommitInfo],
    vault_changed_notes: list[VaultNote],
    vault_knowledge: VaultKnowledge,
    templates: dict[str, str],
    project_name: str,
    retry_error: str | None = None,
    previous_actions: list[dict[str, Any]] | None = None,
) -> str:
    """User prompt dla FINALIZE (tryb multi-turn, po zebraniu wszystkich podsumowan).

    Wstawia **zgromadzone podsumowania** pogrupowane per plik (zamiast
    pelnych diffow), pelny kontekst vaulta, szablony. AI ma wywolac
    ``submit_plan`` DOKLADNIE RAZ.

    :param chunked_commit: pelny ``ChunkedCommit`` (commit meta + lista chunkow)
    :param chunk_summaries: podsumowania kazdego chunka \u2014 lista w tej samej
        kolejnosci co ``chunked_commit.chunks`` (1:1 mapping). Elementy
        moga pochodzic z ``ChunkCache`` lub swiezego wywolania AI.
    :param vault_changes, vault_changed_notes, vault_knowledge, templates,
        project_name, retry_error, previous_actions: jak w ``build_user_prompt``.
    """

    sections: list[str] = []

    sections.append(_section_retry_error(retry_error))
    sections.append(_section_previous_actions(previous_actions))
    sections.append(_section_project_context(project_name))
    sections.append(_section_commit_meta(chunked_commit.commit))
    sections.append(_section_file_changes_overview(chunked_commit))
    sections.append(_section_chunk_summaries(chunked_commit, chunk_summaries))
    sections.append(_section_vault_changes(vault_changes, vault_changed_notes))
    sections.append(_section_vault_knowledge(vault_knowledge))
    sections.append(_section_templates(templates))
    sections.append(_section_task_finalize())

    return "\n\n".join(s for s in sections if s.strip())


def _section_retry_error(retry_error: str | None) -> str:
    if not retry_error:
        return ""
    return (
        "## PONOWNA PROBA \u2014 Twoja poprzednia odpowiedz byla niepoprawna\n\n"
        f"Blad walidacji: {retry_error}\n\n"
        "Popraw sie w tej sesji: kazda sciezka musi byc relatywna, konczyc sie "
        "na `.md` i nie wychodzic poza vault (bez `..`, bez absolutnych sciezek). "
        "Zakoncz **natychmiast** wywolaniem `submit_plan(summary=...)` \u2014 "
        "ta proba jest retry, masz juz wiedze z poprzedniej rundy (ponizej). "
        "**Nie rob ponownie eksploracji ani redundantnych write'ow**: jedynie "
        "uzupelnij to, czego brakuje, i finalizuj."
    )


def _section_previous_actions(
    previous_actions: list[dict[str, Any]] | None,
) -> str:
    """Render listy wykonanych tool callow z poprzedniej proby (retry only).

    Kontekst: ``ToolExecutionContext`` zyje per pojedyncza sesja tool-use.
    Kiedy sesja padnie (``max_tool_iterations`` wyczerpane bez ``submit_plan``),
    cala lista ``proposed_writes`` idzie do kosza wraz z ctx \u2014 retry zaczyna
    od zera. Ta sekcja przekazuje modelowi **co juz probowal w poprzedniej
    rundzie** (na podstawie ``executed_actions``), zeby nie powtarzal tej
    samej eksploracji.

    Format: krotka tabelka, max 30 pozycji (pozniejsze uciete). Kazdy wpis
    zawiera narzedzie, sciezke (jesli byla) i wynik (ok/failed). Bez pelnych
    args \u2014 za duzo tokenow, model i tak widzi aktualny stan vaulta.
    """

    if not previous_actions:
        return ""

    lines: list[str] = [
        "## Poprzednia proba \u2014 co juz zrobiles (kontekst retry)",
        "",
        "Ponizej lista tool callow z **poprzedniej proby** tej sesji. Sesja "
        "zostala przerwana (np. brak `submit_plan` w budzecie iteracji), wiec "
        "ctx zostal zresetowany \u2014 ale logicznie te akcje byly juz proponowane. "
        "Traktuj to jak swoja pamiec: **nie powtarzaj eksploracji** (`list_notes`, "
        "`read_note`, `find_related`) i **nie duplikuj write'ow** o tej samej "
        "semantyce. Idz prosto do uzupelnienia brakujacych rzeczy i `submit_plan`.",
        "",
        "| # | Narzedzie | Sciezka | Wynik |",
        "|---|-----------|---------|-------|",
    ]

    max_rows = 30
    for idx, action in enumerate(previous_actions[:max_rows], start=1):
        tool_name = str(action.get("tool") or "?")
        path = str(action.get("path") or "-")
        result = str(action.get("result") or "?")
        lines.append(f"| {idx} | `{tool_name}` | `{path}` | {result} |")

    overflow = len(previous_actions) - max_rows
    if overflow > 0:
        lines.append("")
        lines.append(f"_... i jeszcze {overflow} akcji (ucieto dla oszczednosci tokenow)._")

    lines.append("")
    lines.append(
        "> **Stan vaulta powyzej odzwierciedla swiezy skan** \u2014 jesli w tabeli "
        "widzisz `create_*` na sciezce, ktora w `list_notes` jeszcze nie istnieje, "
        "oznacza to, ze ta akcja nie zostala zaaplikowana (ctx zresetowany). "
        "Powtorz ja w tej probie, bo bez tego commit nie zostanie udokumentowany."
    )
    return "\n".join(lines)


def _section_project_context(project_name: str) -> str:
    return (
        "## Kontekst projektu\n\n"
        f"Pracujesz nad projektem: **{project_name}**. "
        "Twoim zadaniem jest aktualizacja dokumentacji w vaulcie Obsidiana "
        "na podstawie JEDNEGO commita projektowego ponizej."
    )


def _section_commit_meta(commit: CommitInfo) -> str:
    """Tylko meta commita \u2014 bez diffow, bez listy plikow. Wspolne dla wszystkich trybow."""

    lines: list[str] = [
        "## Commit projektowy",
        "",
        f"- **SHA:** `{commit.sha}`",
        f"- **Data:** {commit.date.isoformat()}",
        f"- **Autor:** {commit.author}",
        f"- **Statystyki:** +{commit.stats.insertions} / -{commit.stats.deletions}",
        "- **Wiadomosc:**",
        "",
    ]
    for msg_line in commit.message.splitlines() or [""]:
        lines.append(f"  > {msg_line}")
    return "\n".join(lines)


def _section_file_changes_overview(chunked: ChunkedCommit) -> str:
    """Lista plikow + do ktorych chunkow trafil (chunki moga mieszac pliki)."""

    lines = [
        "## Zmienione pliki (po chunkingu diffow)",
        "",
        f"- Lacznie plikow: {len(chunked.commit.changes)}",
        f"- Lacznie chunkow: {chunked.total_chunks}",
    ]
    if chunked.skipped_files:
        lines.append(f"- Pominiete przez ignore_patterns: {len(chunked.skipped_files)}")
    lines.append("")

    path_to_chunk_ids: dict[str, list[int]] = {}
    for chunk in chunked.chunks:
        for path in chunk.file_paths:
            path_to_chunk_ids.setdefault(path, []).append(chunk.chunk_idx)

    for change in chunked.commit.changes:
        path = change.path
        chunk_ids = path_to_chunk_ids.get(path, [])
        chunks_str = ", ".join(f"#{cid}" for cid in chunk_ids) if chunk_ids else "-"
        old = f" (dawniej: `{change.old_path}`)" if change.old_path else ""
        lines.append(
            f"- `[{change.change_type.value}]` `{path}`{old} "
            f"\u2014 w chunkach: {chunks_str}"
        )
    if chunked.skipped_files:
        lines.append("")
        lines.append("_Pominiete:_ " + ", ".join(f"`{p}`" for p in chunked.skipped_files[:20]))
        if len(chunked.skipped_files) > 20:
            lines.append(f"_... i jeszcze {len(chunked.skipped_files) - 20}_")
    return "\n".join(lines)


def _section_commit_with_chunks(chunked: ChunkedCommit) -> str:
    """Dla trybu SMALL (1 chunk) \u2014 sklada meta + file overview + pelny diff.

    Gdy chunkow wiecej niz 1 \u2014 to blad logiki (small-path jest tylko
    dla total_chunks == 1), ale renderujemy wszystko zeby nie zgubic tresci.
    """

    sections = [_section_commit_meta(chunked.commit), _section_file_changes_overview(chunked)]
    if chunked.chunks:
        sections.append("## Diff (calosc w jednym chunku)")
        sections.append("")
        for chunk in chunked.chunks:
            files_line = ", ".join(f"`{p}`" for p in chunk.file_paths) or "_(brak)_"
            sections.append(
                f"**Chunk {chunk.chunk_idx}/{chunk.total_chunks}** "
                f"(id=`{chunk.chunk_id}`) \u2014 pliki: {files_line}\n\n"
                f"```diff\n{chunk.diff_text.rstrip()}\n```"
            )
    return "\n\n".join(sections)


def _section_chunk_summaries(
    chunked: ChunkedCommit,
    summaries: list[ChunkSummary],
) -> str:
    """Podsumowania wszystkich chunkow po kolei (chunk 1, chunk 2, ...).

    Chunki moga mieszac fragmenty wielu plikow, wiec grupowanie per plik
    nie ma sensu \u2014 raportujemy je w kolejnosci chunk_idx. Dla kazdego
    podajemy liste plikow, chunk_id, i (gdy to split-hunk) metadane grupy
    splitu \u2014 AI wtedy wie ze chunki X i Y naleza do tego samego logicznego hunka.
    """

    if not summaries:
        return (
            "## Podsumowania chunkow\n\n"
            "_(brak podsumowan \u2014 commit nie zawieral zmian tekstowych do analizy)_"
        )

    summary_by_chunk_idx: dict[int, ChunkSummary] = {
        chunked.chunks[idx].chunk_idx: summary
        for idx, summary in enumerate(summaries)
        if idx < len(chunked.chunks)
    }

    lines: list[str] = [
        "## Podsumowania chunkow (analiza z poprzednich tur)",
        "",
        "Ponizej podsumowania **kazdego chunka** calego commita, w kolejnosci. "
        "Chunki moga laczyc hunki z kilku plikow (grupowanie oszczedzajace "
        "tokeny). To sa TWOJE analizy z poprzednich tur rozmowy \u2014 potraktuj "
        "je jako jedno spojne wyobrazenie commita.",
        "",
        "**UWAGA o split-hunk:** chunki oznaczone `split` z tym samym "
        "`split_group` to kawalki **jednego logicznego hunka** podzielonego "
        "po liniach \u2014 analizuj je jako calosc, nie osobne zmiany.",
        "",
    ]

    for chunk in chunked.chunks:
        summary = summary_by_chunk_idx.get(chunk.chunk_idx)
        files_line = ", ".join(f"`{p}`" for p in chunk.file_paths) or "_(brak)_"
        header = (
            f"### Chunk {chunk.chunk_idx}/{chunk.total_chunks} "
            f"(id=`{chunk.chunk_id}`) \u2014 pliki: {files_line}"
        )
        if chunk.is_split:
            header += (
                f" _(split {chunk.split_part}/{chunk.split_total}, "
                f"grupa=`{chunk.split_group}`)_"
            )
        lines.append(header)
        lines.append("")
        if summary is not None:
            lines.append(f"> {summary.summary.strip()}")
        else:
            lines.append("> _(brak podsumowania \u2014 chunk pominiety)_")
        lines.append("")
    return "\n".join(lines)


def _section_vault_changes(
    vault_changes: list[CommitInfo],
    vault_changed_notes: list[VaultNote],
) -> str:
    if not vault_changes and not vault_changed_notes:
        return (
            "## Zmiany recznie zrobione w vaulcie\n\n"
            "_(brak \u2014 uzytkownik nic recznie nie zmienil w dokumentacji "
            "od ostatniego biegu agenta)_"
        )

    lines: list[str] = ["## Zmiany recznie zrobione w vaulcie od ostatniego biegu", ""]
    lines.append(
        "Ponizej commity z repo vaulta wykonane recznie przez uzytkownika. "
        "Traktuj je jako kontekst \u2014 co czlowiek sam uznal za istotne."
    )
    lines.append("")

    if vault_changes:
        lines.append("### Commity w vaulcie")
        for c in vault_changes[:20]:
            first_msg = c.message.strip().split("\n", 1)[0][:80]
            lines.append(f"- `{c.sha[:7]}` {first_msg} _(autor: {c.author})_")
        if len(vault_changes) > 20:
            lines.append(f"- _... i jeszcze {len(vault_changes) - 20} wczesniejszych_")
        lines.append("")

    if vault_changed_notes:
        lines.append("### Aktualne tresci zmienionych notatek")
        lines.append("")
        for note in vault_changed_notes[:20]:
            lines.append(_format_note_block(note))
            lines.append("")
        if len(vault_changed_notes) > 20:
            lines.append(
                f"_... i jeszcze {len(vault_changed_notes) - 20} notatek "
                f"\u2014 pominieto dla oszczednosci tokenow_"
            )

    return "\n".join(lines)


def _format_note_block(note: VaultNote) -> str:
    meta = []
    if note.type:
        meta.append(f"type={note.type}")
    if note.parent:
        meta.append(f"parent=[[{note.parent}]]")
    if note.tags:
        meta.append(f"tags={note.tags}")
    meta_str = f" ({', '.join(meta)})" if meta else ""

    body_preview = note.content.strip()
    if len(body_preview.splitlines()) > 40:
        body_preview = "\n".join(body_preview.splitlines()[:40]) + "\n... _(tresc skrocona)_"

    return (
        f"**`{note.path}`**{meta_str}\n\n"
        f"```markdown\n{body_preview}\n```"
    )


def _section_vault_knowledge(knowledge: VaultKnowledge) -> str:
    """Mapa wiedzy — **tylko top-level** (Faza 4 refaktoru).

    Do Fazy 3 sekcja dumpowala pelna liste notatek (path | type | parent)
    do 250 wpisow. Od Fazy 4 zostaje tylko **skompresowana mapa**:

    - liczniki per type (``hub: 12, decision: 38, module: 15, ...``)
    - liczniki meta (total_notes, all_tags, orphaned_links)
    - lista MOC-ow (z iloscia dzieci)
    - lista hubow (max ``_MAX_HUB_LIST``)

    Pozostale informacje (konkretne notatki, backlinki, tresc) agent
    pobiera **on-demand** przez narzedzia eksploracyjne:
    ``list_notes`` / ``read_note`` / ``find_related`` / ``list_pending_concepts``.

    Dzieki temu prompt jest staly w rozmiarze niezaleznie od rozmiaru
    vaulta (rosnie tylko logarytmicznie przez liczniki) — a prompt caching
    lapie gesty, powtarzalny prefiks.
    """

    lines: list[str] = [
        "## Aktualny stan vaulta \u2014 mapa wiedzy (top-level)",
        "",
        f"- **Lacznie notatek:** {knowledge.total_notes}",
        f"- **MOC-ow:** {len(knowledge.moc_files)}",
        f"- **Unikalne tagi:** {len(knowledge.all_tags)}",
        f"- **Osierocone wikilinki:** {len(knowledge.orphaned_links)}",
        "",
        "> Szczegoly (konkretne notatki, tresci, backlinki) pobieraj **on-demand** "
        "przez narzedzia: `list_notes`, `read_note`, `find_related`, "
        "`list_pending_concepts`. Ta sekcja to TYLKO mapa najwyzszego poziomu.",
        "",
    ]

    if knowledge.by_type:
        counts = sorted(
            ((t, len(paths)) for t, paths in knowledge.by_type.items()),
            key=lambda pair: (-pair[1], pair[0]),
        )
        counts_str = ", ".join(f"`{t}`: {n}" for t, n in counts)
        lines.append(f"- **Notatki per type:** {counts_str}")
        lines.append("")

        lines.append("### Typy — przykladowe notatki (zanim zaproponujesz duplikat)")
        for note_type, total in counts:
            paths = sorted(knowledge.by_type.get(note_type, []))
            stems = [Path(p).stem for p in paths[:_MAX_TYPE_EXAMPLES]]
            overflow = total - len(stems)
            stems_str = ", ".join(f"`[[{s}]]`" for s in stems) if stems else "-"
            suffix = f" _(+{overflow} wiecej — uzyj `list_notes(type='{note_type}')`)_" if overflow > 0 else ""
            lines.append(f"- **`{note_type}`** ({total}): {stems_str}{suffix}")
        lines.append("")

    if knowledge.by_tag:
        top_tags = sorted(
            ((tag, len(paths)) for tag, paths in knowledge.by_tag.items()),
            key=lambda pair: (-pair[1], pair[0]),
        )
        visible = top_tags[:_MAX_TOP_TAGS]
        tags_str = ", ".join(f"`{t}`: {n}" for t, n in visible)
        overflow = len(top_tags) - len(visible)
        suffix = f" _(+{overflow} rzadszych — uzyj `list_tags` zeby zobaczyc pelna mape)_" if overflow > 0 else ""
        lines.append(f"- **Top tagi** (top {len(visible)}/{len(top_tags)}): {tags_str}{suffix}")
        lines.append("")

    mocs = knowledge.mocs()
    if mocs:
        lines.append("### MOC-i (uzyj ich w `parent` nowych notatek)")
        for moc in mocs:
            children_count = len(knowledge.children_of(Path(moc.path).stem))
            lines.append(
                f"- `[[{Path(moc.path).stem}]]` ({moc.path}) \u2014 {children_count} dzieci"
            )
        lines.append("")

    hubs = knowledge.find_by_type("hub")
    if hubs:
        hubs_sorted = sorted(hubs, key=lambda n: n.path)
        limit_exceeded = len(hubs_sorted) > _MAX_HUB_LIST
        subset = hubs_sorted[:_MAX_HUB_LIST]
        lines.append("### Huby (`type: hub`) \u2014 wezly tematyczne")
        for hub in subset:
            parent_str = f" \u2190 [[{hub.parent}]]" if hub.parent else ""
            lines.append(f"- `[[{Path(hub.path).stem}]]` ({hub.path}){parent_str}")
        if limit_exceeded:
            skipped = len(hubs_sorted) - _MAX_HUB_LIST
            lines.append(
                f"- _... i jeszcze {skipped} hubow \u2014 uzyj `list_notes(type='hub')`._"
            )
        lines.append("")

    if knowledge.orphaned_links:
        lines.append("### Osierocone wikilinki (placeholdery \u2014 kandydaci do wypelnienia)")
        preview = ", ".join(knowledge.orphaned_links[:20])
        if len(knowledge.orphaned_links) > 20:
            preview += f" ... (+{len(knowledge.orphaned_links) - 20} \u2014 uzyj `list_pending_concepts`)"
        lines.append(preview)

    return "\n".join(lines)


def _section_templates(templates: dict[str, str]) -> str:
    if not templates:
        return ""

    lines: list[str] = [
        "## Szablony notatek (referencja struktury)",
        "",
        "Ponizej szablony dla rozpoznawalnych typow. Gdy tworzysz notatke "
        "danego typu, jej frontmatter i sekcje powinny odpowiadac szablonowi. "
        "Placeholdery `{{title}}`, `{{date}}`, `{{commit_short_sha}}`, "
        "`{{commit_subject}}`, `{{commit_author}}`, `{{commit_date}}` "
        "zastapisz konkretnymi wartosciami z commita.",
        "",
    ]
    for name in ("changelog", "adr", "module", "doc"):
        if name not in templates:
            continue
        lines.append(f"### Szablon `{name}`")
        lines.append("")
        lines.append("```markdown")
        lines.append(templates[name].rstrip())
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def _section_task_small() -> str:
    return (
        "## Zadanie\n\n"
        "Przeanalizuj powyzszy commit projektowy w kontekscie stanu vaulta i "
        "zmian recznych. Pracujesz w **petli tool-use** \u2014 wywoluj kolejno "
        "narzedzia `create_note` / `update_note` / `append_to_note`, aby "
        "zarejestrowac kazda propozycje zmiany w vaulcie (nic nie jest "
        "zapisywane natychmiast \u2014 zmiany trafiaja do bufora propozycji).\n\n"
        "Sesje ZAWSZE konczysz wywolaniem `submit_plan(summary=\"...\")`, "
        "nawet jesli nie zarejestrowales zadnej akcji (pusty plan jest "
        "dozwolony gdy commit nic nie wnosi dokumentacyjnie \u2014 "
        "w `summary` wyjasnij dlaczego)."
    )


def _section_task_finalize() -> str:
    return (
        "## Zadanie (FINALIZE)\n\n"
        "Masz juz powyzej **zebrane podsumowania WSZYSTKICH chunkow** tego commita. "
        "Na ich podstawie, wraz z aktualnym stanem vaulta i recznymi zmianami "
        "usera, zaproponuj plan dokumentacji wywolujac kolejno narzedzia "
        "`create_note` / `update_note` / `append_to_note`.\n\n"
        "Sesje ZAWSZE konczysz wywolaniem `submit_plan(summary=\"...\")`, "
        "nawet jesli nie zarejestrowales zadnej akcji \u2014 pusta propozycja "
        "jest dozwolona, gdy commit nic nie wnosi do dokumentacji "
        "(w `summary` wyjasnij dlaczego)."
    )
