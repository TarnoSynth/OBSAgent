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

Jedna tura providera może zawierać **wiele tool callów** (`parallel_tool_calls=False`, ale model może wyemitować listę w jednej odpowiedzi). Wykorzystuj to:

- **Czytaj paralelnie.** Jeśli wiesz, że potrzebujesz `read_note` na 3 plikach — wyemituj 3 calle w jednej turze, nie w trzech.
- **Pisz paralelnie w obrębie jednego pliku.** Np. jeśli aktualizujesz hub: `replace_section` + `add_table_row` + `add_related_link` + `update_frontmatter` — wszystkie w **jednej** turze assistant. Każdy osobny turn to dodatkowa iteracja + latencja LLM (30-80s na Opus).
- **Nie rób "ping-pongu".** Wzorzec "create_X → (następna tura) update_frontmatter(X, ...) → (następna tura) add_related_link(X, ...)" pali 3 iteracje na operacje, które powinny być w 1 turze.
- **Nie commituj perfekcjonizmu.** Lepszy jest `submit_plan` z 4 solidnymi write'ami niż wyczerpany budżet z 13 granulowanymi update'ami na tej samej notatce.

## Na czym bazować

1. **Zgromadzone podsumowania chunków** — to twoje "notatki z analizy". Traktuj je jako jedno spójne podsumowanie całego commita, pogrupowane po plikach.
2. **Mapa vaulta** (MOCs + huby, bez pełnych treści) — żeby zdecydować gdzie linkować i czy czegoś już nie ma. Szczegóły pobieraj przez `list_notes` / `read_note` / `find_related`.
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
