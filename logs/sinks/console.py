"""Console sink — pretty-print wybranych eventow do stderr (rich).

Nie duplikuje tego, co juz wypisuje UI w ``main.py`` (Panel, rule, chunk
progress). Zajmuje sie **tylko** tym, czego user normalnie nie widzi:

- ``llm.call.failed``   — pelny komunikat bledu z statusem i fragmentem body
- ``llm.call.ok``       — krotkie podsumowanie (tylko dla DEBUG / bardzo
                           wysokiego poziomu szczegolowosci)
- ``run.started`` / ``run.ended`` — banner z run_id i sciezka do JSONL

Dzieki temu po crashu typu "Blad komunikacji z Anthropic API:" user od
razu widzi CO naprawde padlo, zamiast pustego stringu.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console

from logs.events import Event


class ConsoleSink:
    """Prints wybranych eventow do stderr przez ``rich``."""

    name = "console"

    def __init__(self, console: Console | None = None, *, verbose: bool = False) -> None:
        self._console = console or Console(stderr=True)
        self._verbose = verbose

    def emit(self, event: Event) -> None:
        try:
            self._dispatch(event)
        except Exception:
            pass

    def _dispatch(self, event: Event) -> None:
        payload = event.payload
        t = event.type

        if t == "run.started":
            run_id = event.run_id
            jsonl = payload.get("jsonl_path") or "(brak)"
            self._console.print(
                f"[dim]>> logger start: run_id={run_id} jsonl={jsonl}[/]"
            )
            return

        if t == "run.ended":
            processed = payload.get("processed_count", 0)
            errors = payload.get("error_count", 0)
            color = "green" if errors == 0 else "yellow"
            self._console.print(
                f"[dim]<< logger end: processed={processed} errors={errors}[/]",
                style=color,
            )
            return

        if t == "llm.call.failed":
            self._print_llm_failure(payload)
            return

        if t == "llm.call.ok" and self._verbose:
            phase = payload.get("phase", "?")
            model = payload.get("model", "?")
            latency = payload.get("latency_ms")
            tokens_in = payload.get("input_tokens")
            tokens_out = payload.get("output_tokens")
            self._console.print(
                f"[dim]llm.ok phase={phase} model={model} "
                f"in={tokens_in} out={tokens_out} {latency}ms[/]"
            )
            return

    def _print_llm_failure(self, payload: dict[str, Any]) -> None:
        phase = payload.get("phase", "?")
        provider = payload.get("provider", "?")
        model = payload.get("model", "?")
        error_type = payload.get("error_type", "Exception")
        error_msg = payload.get("error_message") or "(bez komunikatu)"
        status = payload.get("http_status")
        attempt = payload.get("attempt")
        chunk = payload.get("chunk_idx")
        total = payload.get("chunk_total")

        chunk_tag = f" chunk={chunk}/{total}" if chunk else ""
        attempt_tag = f" attempt={attempt}" if attempt else ""
        status_tag = f" status={status}" if status else ""

        self._console.print(
            f"[red]LLM FAIL[/] [yellow]{phase}[/]{chunk_tag}{attempt_tag} "
            f"[dim]provider={provider} model={model}{status_tag}[/]"
        )
        self._console.print(f"  [red]{error_type}:[/] {error_msg}")

        body_snippet = payload.get("response_body_snippet")
        if body_snippet:
            self._console.print(f"  [dim]body:[/] {body_snippet}")

    def close(self) -> None:
        # Console nic nie trzyma, no-op.
        return
