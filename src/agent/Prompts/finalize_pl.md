# Finalizacja planu dokumentacji (FINALIZE)

Przeanalizowałeś już **wszystkie** fragmenty diffa tego commita w poprzednich turach rozmowy — zwracałeś podsumowania każdego chunka. Te podsumowania są zebrane poniżej razem z mapą vaulta.

Teraz **działaj w pętli tool-use**: woluj narzędzia vault_read (eksploracja) i vault_write (zapis) iteracyjnie, a na końcu wywołaj `submit_plan` DOKŁADNIE RAZ z krótkim `summary`.

## TWARDY KONTRAKT PĘTLI — przeczytaj przed pierwszym tool callem

- **Masz **ograniczony budżet iteracji** (`max_tool_iterations`, zwykle 20).** Każda iteracja = jedno wywołanie providera AI + ewentualne tool cally w środku.
- **`submit_plan` jest jedynym terminatorem.** Bez niego sesja pada z błędem walidacji, plan idzie do kosza, i retry uruchamia się od zera.
- **Jako pomoc** pętla doklei Ci do `tool_result` informację `[budzet-petli: iteracja X/N, pozostalo M]` gdy zbliżasz się do końca. Gdy widzisz `pozostalo <= 2` — **następna tura powinna być `submit_plan`**.
- **Twarde wymuszenie:** na ostatnich iteracjach provider dostaje `tool_choice={"type":"tool","name":"submit_plan"}` i **nie** pozwoli Ci wywołać niczego innego. Jeśli do tego doszło, coś zrobiłeś źle — zaplanuj zakończenie wcześniej.
- **Typowy, sensowny flow:** 1-2 tury eksploracji read-only → 2-5 tur write → `submit_plan`. Łącznie **5-10 iteracji** dla większości commitów. Jeśli czujesz, że potrzebujesz 15+ — prawdopodobnie duplikujesz robotę.

## Zasada batchowania — oszczędzaj iteracje

Pracujesz z włączonym **`parallel_tool_calls=True`** — w jednej turze **MOŻESZ i POWINIENEŚ** emitować wiele bloków `tool_use` naraz, gdy są niezależne. Każda tura to kolejny call do providera AI z pełnym kontekstem (30–100 s latencji na Opus), więc minimalizuj ich liczbę.

**Kiedy wywołuj równolegle w jednej turze:**

- **Odczyty niezależne.** `read_note` na 3 różnych plikach → 3 tool calls w **jednej** turze, nie w trzech. Tak samo `list_tags` + `vault_map` + `find_related` robione jednocześnie.
- **Zapisy na różnych plikach.** `create_module("A.md")` + `create_module("B.md")` + `create_module("C.md")` → **jedna tura**, nie trzy.
- **Wiele granulowanych operacji na TYM SAMYM pliku.** Aktualizujesz hub? `replace_section` + `add_table_row` + `add_related_link` + `update_frontmatter` → wszystkie w **jednej** turze.
- **Niezależny changelog + moduły.** `create_changelog_entry` + wszystkie `create_module` dla tego commita → **jedna tura**.

**Kiedy MUSISZ iterować sekwencyjnie (osobne tury):**

- Potrzebujesz wyniku narzędzia, żeby zbudować argumenty następnego: np. najpierw `find_related`/`list_notes`, żeby wiedzieć, czy `create_X` czy `replace_section`.
- Najpierw rozpoznanie vaulta (1 tura eksploracji: wiele równoległych `list_notes`/`read_note`), potem faza zapisu (1–2 tury z równoległymi `create_*`/`append_section`), na końcu `submit_plan`.

**Typowy docelowy flow po batchowaniu:** 3–5 iteracji total (1 eksploracja równoległa → 1–2 zapisy równoległe → `submit_plan`). Jeśli widzisz siebie w 10+ iteracjach, to prawie na pewno emitujesz po jednym tool_use na turę zamiast paczkować.

**Anty-wzorce (pal budżet i czas):**

- „Jedno narzędzie na turę" — model zwraca 1 tool_use, czeka na tool_result, zwraca kolejne 1 tool_use. 8 niezależnych `create_module` = 8 iteracji zamiast 1.
- „Ping-pong" — `create_X` → (następna tura) `update_frontmatter(X)` → (następna tura) `add_related_link(X)`. Jeśli wiesz z góry czego chcesz, rób to w 1 turze.
- „Perfekcjonizm granulacji" — 13 drobnych update'ów na tej samej notatce. Rozważ `replace_section` albo `update_note`.

**Gdy walidacja jednego z równoległych wywołań padnie** (np. zły schemat argumentów): pozostałe wywołania z tego samego batcha i tak wrócą z wynikami. W następnej turze popraw TYLKO to, co padło — nie wywołuj ponownie tych, które się udały (dostały już tool_result „ok").

## Na czym bazować

1. **Zgromadzone podsumowania chunków** — to twoje "notatki z analizy". Traktuj je jako jedno spójne podsumowanie całego commita, pogrupowane po plikach.
2. **Mapa vaulta** (MOCs + huby + top-15 tagów + przykładowe stemy per type, bez pełnych treści) — żeby zdecydować gdzie linkować i czy czegoś już nie ma. Szczegóły pobieraj przez `list_notes` (z `include_preview=true` gdy chcesz snippet body) / `read_note` (pełna treść lub wybrane `sections`) / `list_tags` (cała mapa tagów gdy brakuje w top-15) / `vault_map` (hierarchia MOC → hub → moduł) / `find_related` (fuzzy po tematach).
3. **Ręczne zmiany usera w vaulcie** — jeśli user już coś dopisał ręcznie, nie nadpisuj; uwzględnij w planie.
4. **Przykłady typów notatek** (załączone w system prompcie) — używaj ich struktury (frontmatter + sekcje) przy tworzeniu nowych notatek.

## Zasady decyzyjne (przypomnienie)

- Jeden commit = jedna notatka `changelog` (przez `create_changelog_entry`) — chyba że commit jest trywialny i nie wywołujesz żadnego write'a.
- Zmiany architektoniczne → `create_decision` (automatycznie dopisze wiersz do tabeli ADR w rodzicielskim hubie).
- Nowy moduł kodu → `create_module`.
- Nowe pojęcie w dyskusji → `create_concept`; nowa technologia → `create_technology`.
- Generyczne `create_note` / `update_note` są **tylko** dla `type: doc` (wolne dokumenty). Dla typów: `hub`, `concept`, `technology`, `decision`, `module`, `changelog` używaj WYŁĄCZNIE dedykowanych narzędzi.
- Dopinanie wpisów do MOC — przez `add_moc_link` (idempotentne).
- Wikilinki `[[stem]]` zamiast ścieżek; frontmatter zgodny z przykładami.
- Orphan wikilink (wskazujesz na coś bez pliku) → `register_pending_concept`, nie blokuj dokumentacji.

## Format zakończenia

Na końcu sesji wywołaj `submit_plan` z polami:

- `summary`: 1–2 zdania co zrobiłeś w tym commicie i dlaczego (bazując na zebranych podsumowaniach chunków).

`submit_plan` **nie** przyjmuje już listy akcji — one są już zarejestrowane przez indywidualne tool cally. Summary trafia bezpośrednio do preview dla usera oraz do commit message na vaulcie.

**Pusty plan jest dozwolony.** Jeśli commit nic nie wnosi do dokumentacji (bump deps, formatowanie, trywialny bugfix) — NIE rejestruj żadnego write'a, tylko wywołaj `submit_plan(summary="...")` z wyjaśnieniem dlaczego. Idealna ścieżka to 1 iteracja.
