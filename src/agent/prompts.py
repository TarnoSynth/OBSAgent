"""Loader system promptu agenta z ``src/agent/Prompts/``.

System prompt zyje w plikach markdown (`system_pl.md`, `system_en.md`),
wybieranych na podstawie `agent.language` w ``config.yaml``. Dzieki temu
edycja instrukcji dla AI nie wymaga dotykania kodu \u2014 wystarczy zapisac
plik.

Prompty zawieraja placeholder ``{{language}}`` (i potencjalnie inne) \u2014
loader je wypelnia na starcie, przed wyslaniem do providera. Funkcja
jest **pure**: bierze sciezke + context dict, zwraca string.
"""

from __future__ import annotations

from pathlib import Path


PROMPTS_DIR_NAME = "Prompts"
SUPPORTED_LANGUAGES = ("pl", "en")


def load_system_prompt(
    language: str,
    *,
    prompts_dir: Path | None = None,
    examples: dict[str, str] | None = None,
) -> str:
    """Wczytuje system prompt dla zadanego jezyka i wypelnia placeholdery.

    Obsluguje dwa placeholdery:

    - ``{{language}}`` — etykieta jezyka (``polski`` / ``English``).
    - ``{{examples}}`` — blok ``<examples>`` z pelnymi notatkami
      AthleteStack-style (hub, concept, technology, decision, module)
      uzywany jako few-shot. Jesli placeholder istnieje w pliku a
      ``examples`` jest ``None`` — loader sam podciaga je przez
      ``load_all_examples()``. Jesli placeholdera nie ma — blok NIE
      jest doklejany (kompatybilnosc wsteczna).

    :param language: kod jezyka z configu (`pl` / `en`). Inne — ``ValueError``.
    :param prompts_dir: opcjonalnie nadpisanie katalogu z promptami
        (domyslnie: ``src/agent/Prompts/`` obok tego modulu).
    :param examples: opcjonalnie gotowy ``{example_name: raw_markdown}``.
        Gdy prompt zawiera ``{{examples}}`` a argument jest ``None`` —
        ladowane z ``src/agent/templates.load_all_examples()``.
    :return: gotowy system prompt z podstawionymi placeholderami.
    :raises ValueError: przy nieznanym jezyku lub brakujacym pliku.
    """

    lang = (language or "").strip().lower()
    if lang not in SUPPORTED_LANGUAGES:
        raise ValueError(
            f"Nieobslugiwany jezyk system promptu: {language!r}. "
            f"Dozwolone: {', '.join(SUPPORTED_LANGUAGES)}."
        )

    base_dir = prompts_dir or (Path(__file__).resolve().parent / PROMPTS_DIR_NAME)
    prompt_path = base_dir / f"system_{lang}.md"
    if not prompt_path.is_file():
        raise ValueError(
            f"Brak pliku system promptu dla jezyka {lang!r}: {prompt_path}. "
            f"Upewnij sie ze istnieje `system_{lang}.md` w katalogu {base_dir}."
        )

    raw = prompt_path.read_text(encoding="utf-8")
    language_label = _language_label(lang)
    resolved = raw.replace("{{language}}", language_label)

    if "{{examples}}" in resolved:
        if examples is None:
            from src.agent.templates import load_all_examples

            examples = load_all_examples()
        resolved = resolved.replace("{{examples}}", _render_examples_block(examples, lang=lang))

    return resolved


def _render_examples_block(examples: dict[str, str], *, lang: str) -> str:
    """Renderuje blok ``<examples>`` z notatkami AthleteStack-style.

    Kolejnosc kluczy w ``examples`` zachowana (tak jak zwraca
    ``load_all_examples()``). Kazdy przyklad opakowany w tag
    ``<example_{name}>`` — model widzi jednoznacznie ktory przyklad
    odpowiada ktoremu typowi.

    Intro w odpowiednim jezyku (PL/EN) tlumaczy ze to **wzorzec**
    struktury notatki, nie literalnie do skopiowania.
    """

    if not examples:
        return ""

    if lang == "pl":
        intro = (
            "## Przykłady notatek AthleteStack-style (few-shot)\n\n"
            "Poniżej pełne notatki pokazujące **wzorzec** dla każdego typu "
            "obsługiwanego przez dedykowane narzędzia domenowe. Traktuj je "
            "jako **twardy szablon struktury i tonu** — nie kopiuj treści, "
            "zaadaptuj do swojego commita."
        )
    else:
        intro = (
            "## AthleteStack-style note examples (few-shot)\n\n"
            "Below are full notes showing the **pattern** for every type "
            "served by dedicated domain tools. Treat them as a **hard "
            "template for structure and tone** — don't copy content, adapt "
            "it to your commit."
        )

    parts: list[str] = ["<examples>", "", intro, ""]
    for name, content in examples.items():
        parts.append(f"<example_{name}>")
        parts.append("")
        parts.append("```markdown")
        parts.append(content.rstrip())
        parts.append("```")
        parts.append("")
        parts.append(f"</example_{name}>")
        parts.append("")
    parts.append("</examples>")
    return "\n".join(parts)


def load_chunk_instruction_prompt(language: str, *, prompts_dir: Path | None = None) -> str:
    """Wczytuje system prompt dla chunk-summary (tryb multi-turn).

    Krotki prompt informujacy AI, ze dostaje JEDEN fragment diffa i ma
    zwrocic zwiezle podsumowanie (3-6 zdan), bez tool calls. Uzywane
    po kolei dla kazdego chunka duzego commita przed FINALIZE.
    """

    return _load_named_prompt("chunk_instruction", language, prompts_dir)


def load_finalize_prompt(language: str, *, prompts_dir: Path | None = None) -> str:
    """Wczytuje dodatkowy prompt dla FINALIZE multi-turn biegu.

    Stosowany PO pelnym system_prompt agenta \u2014 dodawany jako druga
    wiadomosc system lub wklejony do user promptu. Instruuje AI, zeby
    teraz (po zgromadzeniu podsumowan) wywolal ``submit_plan``
    DOKLADNIE RAZ.
    """

    return _load_named_prompt("finalize", language, prompts_dir)


def _load_named_prompt(name: str, language: str, prompts_dir: Path | None) -> str:
    """Wspolna logika odczytu ``<name>_<lang>.md`` z katalogu Prompts/.

    Taka sama walidacja jezyka i substytucji placeholderow jak w
    ``load_system_prompt`` \u2014 ale DRY, zeby dodanie kolejnych
    prompt-nazw nie wymagalo dupli-kacji.
    """

    lang = (language or "").strip().lower()
    if lang not in SUPPORTED_LANGUAGES:
        raise ValueError(
            f"Nieobslugiwany jezyk promptu {name!r}: {language!r}. "
            f"Dozwolone: {', '.join(SUPPORTED_LANGUAGES)}."
        )

    base_dir = prompts_dir or (Path(__file__).resolve().parent / PROMPTS_DIR_NAME)
    prompt_path = base_dir / f"{name}_{lang}.md"
    if not prompt_path.is_file():
        raise ValueError(
            f"Brak pliku promptu: {prompt_path}. "
            f"Upewnij sie ze istnieje `{name}_{lang}.md` w katalogu {base_dir}."
        )

    raw = prompt_path.read_text(encoding="utf-8")
    return raw.replace("{{language}}", _language_label(lang))


def _language_label(lang: str) -> str:
    if lang == "pl":
        return "polski"
    if lang == "en":
        return "English"
    return lang


__all__ = [
    "PROMPTS_DIR_NAME",
    "SUPPORTED_LANGUAGES",
    "load_chunk_instruction_prompt",
    "load_finalize_prompt",
    "load_system_prompt",
]
