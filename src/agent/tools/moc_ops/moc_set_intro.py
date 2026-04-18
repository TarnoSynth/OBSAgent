"""``moc_set_intro`` - ustawia/aktualizuje blurb MOC (tekst miedzy H1 a pierwszym H2).

**Cel:**

MOC bez opisu to martwa lista linkow. Dobry MOC ma **krotkie wprowadzenie**:
czym jest projekt, jak czytac dokumentacje, gdzie szukac czego. Ten fragment
lezy **miedzy naglowkiem H1** (``# MOC — Kompendium``) **a pierwsza sekcja
H2** (``## Huby``) - to jest "intro".

**Semantyka:**

- Szukamy ``# <heading>\\n`` i pierwszego ``## <section>\\n`` ponizej.
- Cala tresc miedzy nimi zastepujemy nowym ``intro`` (po znormalizowaniu
  whitespace: gwarantujemy pusta linie przed i po).
- Gdy plik nie ma H1 -> blad (MOC MUSI miec tytul).
- Gdy plik nie ma H2 -> intro idzie na koniec pliku (niecodzienny przypadek,
  ale bezpieczny fallback).

**Idempotencja:**

Drugi call z tym samym ``intro`` daje identyczna tresc -> zero zmian.
Sprawdzamy to przez porownanie stringow PRZED rejestracja ``ProposedWrite``.
Nie koalescujemy z innymi toolami - intro to jeden spojny fragment, nie
lista wpisow.

**Kiedy uzywac:**

- Na koncu sesji MOCAgenta - po tym jak dopisal hub-y, technologie i
  koncepty, wpisuje krotkie wprowadzenie opisujace co tam znajdzie usernik.
- Przy odswiezaniu MOC gdy struktura sie zmieniła (nowe sekcje, nowa
  hierarchia hubow).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.agent.tools.base import Tool, ToolResult
from src.agent.tools.context import ToolExecutionContext
from src.agent.tools.vault_write._common import (
    normalize_path_or_error,
    path_exists_effectively,
)
from src.agent.tools.vault_write._granular import (
    compute_effective_content,
    register_granular_update,
)

#: Regex szukajacy pierwszego H1 w tresci (po ewentualnym frontmatterze).
#: Zakladamy standardowy format: linia zaczynajaca sie od ``# ``.
_H1_RE = re.compile(r"^#\s+.+?$", re.MULTILINE)

#: Regex szukajacy pierwszego H2 ponizej H1.
_H2_RE = re.compile(r"^##\s+.+?$", re.MULTILINE)


def _normalize_intro(text: str) -> str:
    """Normalizuje intro: strip skrajnych pustych linii, ensure trailing newline."""

    stripped = text.strip()
    if not stripped:
        return ""
    return stripped + "\n"


def _replace_intro(raw: str, new_intro: str) -> tuple[str, bool]:
    """Zastepuje intro w MOC-u. Zwraca (new_raw, changed).

    changed=False gdy intro juz jest takie samo (idempotencja).
    Rzuca ``ValueError`` gdy brak H1 (invalid MOC).
    """

    h1_match = _H1_RE.search(raw)
    if h1_match is None:
        raise ValueError(
            "MOC nie ma naglowka H1 (linia zaczynajaca sie od '# '). "
            "Dodaj tytul przez update_note / create_note zanim ustawisz intro."
        )

    h1_end = h1_match.end()

    # szukamy pierwszego H2 po H1
    h2_match = None
    for m in _H2_RE.finditer(raw, h1_end):
        h2_match = m
        break

    if h2_match is None:
        intro_slice_end = len(raw)
    else:
        intro_slice_end = h2_match.start()

    current_intro_raw = raw[h1_end:intro_slice_end]

    normalized_new = _normalize_intro(new_intro)
    if normalized_new:
        new_segment = "\n\n" + normalized_new
    else:
        new_segment = "\n\n"

    current_segment = current_intro_raw

    before = raw[:h1_end]
    after = raw[intro_slice_end:]

    new_raw = before + new_segment + after

    # gwarantujemy ze przed H2 jest jedna pusta linia (nie wiecej, nie mniej)
    if h2_match is not None:
        new_raw = re.sub(r"\n{3,}##", "\n\n##", new_raw, count=1)

    return new_raw, new_raw != raw


class _MocSetIntroArgs(BaseModel):
    """Argumenty ``moc_set_intro``."""

    model_config = ConfigDict(extra="forbid")

    moc_path: str = Field(
        ...,
        min_length=1,
        description=(
            "Sciezka relatywna do pliku MOC (np. 'MOC___Kompendium.md'). "
            "Plik musi istniec w vaulcie albo byc proponowany przez "
            "wczesniejszy create_note w tej sesji."
        ),
    )
    intro: str = Field(
        ...,
        description=(
            "Tresc intro w markdown - 1-5 akapitow. Bedzie wstawiona miedzy "
            "naglowek H1 ('# Tytul MOC') a pierwsza sekcja H2 ('## Huby'). "
            "Mozesz uzywac wikilinkow [[...]], inline kodu, list, czego "
            "potrzebujesz. Whitespace na koncach jest strippany - nie martw "
            "sie o trailing newline. Pusty string kasuje intro."
        ),
    )


class MocSetIntroTool(Tool):
    """Ustawia/aktualizuje intro (blurb) MOC-a - tekst miedzy H1 a pierwsza sekcja H2."""

    name = "moc_set_intro"
    description = (
        "Zastepuje tresc miedzy naglowkiem H1 ('# Tytul') a pierwsza sekcja H2 "
        "('## Huby') w pliku MOC. Uzywaj do dodania/aktualizacji wprowadzenia - "
        "krotkiego opisu co to za MOC i jak go czytac. Idempotentne - drugi call "
        "z tym samym intro nie robi nic. NIE zmienia sekcji ponizej - to ciezka "
        "operacja, uzywaj raz na koncu sesji albo przy duzym refactorze MOC."
    )

    def input_schema(self) -> dict[str, Any]:
        return _MocSetIntroArgs.model_json_schema()

    async def execute(
        self,
        args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        try:
            parsed = _MocSetIntroArgs.model_validate(args)
        except Exception as exc:
            return ToolResult(ok=False, error=f"Walidacja argumentow padla: {exc}")

        normalized = normalize_path_or_error(parsed.moc_path)
        if isinstance(normalized, ToolResult):
            return normalized

        if not path_exists_effectively(ctx, normalized):
            return ToolResult(
                ok=False,
                error=(
                    f"MOC path does not exist: {normalized!r} - stworz MOC przez "
                    "create_note zanim ustawisz jego intro."
                ),
            )

        current = compute_effective_content(ctx, normalized)
        if current is None:
            return ToolResult(
                ok=False,
                error=f"Nie udalo sie odczytac biezacej tresci {normalized!r}.",
            )

        try:
            new_content, changed = _replace_intro(current, parsed.intro)
        except ValueError as exc:
            return ToolResult(ok=False, error=str(exc))

        if not changed:
            ctx.record_action(
                tool=self.name,
                path=normalized,
                args={"result": "noop_same_intro"},
                ok=True,
            )
            return ToolResult(
                ok=True,
                content=(
                    f"Intro w {normalized!r} juz jest identyczne - no-op (idempotencja)."
                ),
            )

        return register_granular_update(
            ctx=ctx,
            tool_name=self.name,
            normalized_path=normalized,
            new_content=new_content,
            op_summary=f"SET_INTRO ({len(parsed.intro)} znakow)",
            extra_log_args={"intro_length": len(parsed.intro)},
        )


__all__ = ["MocSetIntroTool"]
