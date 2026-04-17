# Finalizacja planu dokumentacji (FINALIZE)

Przeanalizowałeś już **wszystkie** fragmenty diffa tego commita w poprzednich turach rozmowy — zwracałeś podsumowania każdego chunka. Te podsumowania są zebrane poniżej razem z kontekstem vaulta i szablonów.

Teraz **finalizuj pracę**: wywołaj narzędzie `submit_plan` DOKŁADNIE RAZ, z planem akcji dla vaulta dokumentacji.

## Na czym bazować

1. **Zgromadzone podsumowania chunków** — to twoje "notatki z analizy". Traktuj je jako jedno spójne podsumowanie całego commita, pogrupowane po plikach.
2. **Stan vaulta** (aktualne notatki, MOC, zasoby) — żeby nie duplikować dokumentacji i linkować do istniejących wpisów.
3. **Ręczne zmiany usera w vaulcie** — jeśli user już coś dopisał ręcznie, nie nadpisuj; uwzględnij w planie.
4. **Szablony notatek** — używaj ich struktury (frontmatter + sekcje) przy tworzeniu nowych notatek.

## Zasady decyzyjne (przypomnienie)

- Jeden commit = jedna notatka typu `changelog` (chyba że commit jest trywialny — pusta lista `actions` jest wtedy OK)
- Zmiany architektoniczne → nowa notatka typu `adr`
- Nowy moduł kodu → nowa notatka typu `module`
- MOC (`MOC__*.md`) i indeks (`_index.md`) **nie są** w planie — planner agenta sam je zaktualizuje
- Wikilinki `[[stem]]` zamiast ścieżek; frontmatter zgodny z szablonami

## Format odpowiedzi

Wywołaj `submit_plan` z argumentami:

- `summary`: 1-2 zdania co robisz w tym commicie i dlaczego (bazując na zebranych podsumowaniach)
- `actions`: lista `AgentAction` z polami `type` (`create`/`update`/`append`), `path` (relatywna, `.md`), `content` (pełna treść dla create/update, sam dopisek dla append)

Nie pisz nic poza wywołaniem narzędzia.
