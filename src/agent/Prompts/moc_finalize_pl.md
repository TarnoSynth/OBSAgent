# Finalizacja sesji MOCAgenta

Przeanalizowałeś raport `moc_audit`, stworzyłeś potrzebne huby/
technologie/koncepty, dopiąłeś je do MOC-u, uzupełniłeś intro. Czas
zamknąć sesję.

## TWARDY KONTRAKT

- **Masz ograniczony budżet iteracji** (`max_tool_iterations`).
- **`submit_plan` jest jedynym terminatorem.** Bez niego sesja pada z
  błędem, plan idzie do kosza, retry od zera.
- **Twarde wymuszenie:** na ostatnich iteracjach provider dostaje
  `tool_choice={"type":"tool","name":"submit_plan"}` i nie pozwoli
  wywołać niczego innego.
- **Typowy flow:** 1 iteracja `moc_audit` → 2-5 iteracji `create_*` +
  `add_moc_link` + `add_related_link` (każda paczka równoległa) → 1
  iteracja `moc_set_intro` → `submit_plan`. Łącznie **5-10 iteracji**.

## Batchuj równolegle

`parallel_tool_calls=True` — w jednej turze emituj wiele niezależnych
wywołań:

- **Wszystkie `create_hub` naraz** (6 hubów = 1 tura).
- **Wszystkie `create_technology` naraz** po tym jak wiesz jakie.
- **Wszystkie `add_moc_link` naraz** — dopinanie hubów/technologii/
  konceptów do sekcji MOC.
- **Wszystkie `add_related_link` naraz** — dolinkowanie hubów z ich
  modułami.

Anty-wzorzec: jeden `create_hub` → tura → drugi `create_hub` → tura. To
pali budżet na próżno.

## Gdy audyt pokazał "wszystko OK"

Jeśli raport `moc_audit` nie dał żadnych sugestii hubów, orphanów ani
brakujących sekcji — **nie wymyślaj akcji na siłę**. Wywołaj od razu:

```
submit_plan(summary="MOC audyt czysty — brak zmian.")
```

i kończymy. Pusta sesja jest w pełni dozwolona.

## Format `submit_plan`

- `summary`: 1-3 zdania co zrobiłeś i dlaczego (np. "Dodano huby
  tematyczne Agent, Git, Logs, Mcp, Providers, Vault grupujące 54
  moduły. Utworzono 8 notatek technologii: FastAPI, Pydantic, httpx,
  openai, anthropic, rich, pyyaml, GitPython. Uzupełniono intro MOC.").

`submit_plan` **nie przyjmuje listy akcji** — są już zarejestrowane
przez indywidualne tool cally. Summary trafia do preview + commit
message na vaulcie (prefix `Agent-MOC:`).
