---
tags:
  - concept
  - architecture
type: concept
parent: "[[Architektura_systemu]]"
related:
  - "[[Mikroserwisy]]"
  - "[[Modul_kodu]]"
  - "[[Event_Bus]]"
status: active
created: 2026-03-14
updated: 2026-03-14
---

# Modularny monolit

**Definicja:** jedna aplikacja deployowana jako pojedynczy binarny/proces,
ktorej kod jest podzielony na wyraznie izolowane moduly z oddzielnymi
modelami domenowymi, warstwami danych i kontraktami. W przeciwienstwie do
mikroserwisow - nie ma granicy sieciowej miedzy modulami.

## Kontekst

AthleteStack startowal jako monolit Django. Pokusa rozbicia na mikroserwisy
byla silna (team rosl), ale koszt operacyjny (service mesh, distributed
tracing, eventual consistency) byl **znacznie wyzszy** niz zysk z
niezaleznego deploymentu. Po [[ADR__UseModularMonolith]] zdecydowalismy,
ze graniczymy moduly **kontraktowo** (publiczne API per modul, brak importu
private), a nie **sieciowo** (serwisy w osobnych procesach).

Zastosowanie:

- [[Backend_Django]] - cale dzialania domenowe trzymamy w jednym procesie.
- [[Embedding_Pipeline]] - modul z wlasnym API, ale deploy razem z reszta.
- [[Auth]] - ma publiczny interface ``AuthFacade``, reszta kodu go wola.

## Alternatywy odrzucone

| Alternatywa                  | Dlaczego odrzucona                                          |
|------------------------------|-------------------------------------------------------------|
| [[Mikroserwisy]]             | Service mesh + distributed tracing, team ~5 osob - za ciezkie. |
| Monolit bez modulow          | Brak granic => coupling rosnie z kazdym sprintem.           |
| Serverless (FaaS per feature)| Cold start, vendor lock-in, trudno testowac integracyjnie.  |

## Powiazane notatki

- [[Mikroserwisy]] - concept, ktory jest przeciwienstwem tej decyzji.
- [[Modul_kodu]] - jak dokumentujemy pojedynczy modul w modularnym monolicie.
- [[ADR__UseModularMonolith]] - twarda decyzja architektoniczna.
