---
tags:
  - hub
  - architecture
type: hub
parent: "[[MOC___Kompendium]]"
related:
  - "[[MOC___Architektura]]"
status: active
created: 2026-03-14
updated: 2026-04-18
---

# Architektura systemu AthleteStack

AthleteStack to **modularny monolit** z wyraznie zarysowanymi granicami
domenowymi. Ten hub agreguje wiedze o warstwach systemu, decyzjach
ksztaltujacych architekture oraz kluczowych modulach i technologiach.
Zyje pod [[MOC___Architektura]] jako "single page entry" dla kazdego,
kto chce zrozumiec **jak** AthleteStack jest zbudowany i **dlaczego**.

## Przeglad

Projekt sklada sie z trzech warstw: **prezentacja** ([[Frontend_Next]]),
**logika domenowa** ([[Backend_Django]]) i **persystencja** ([[Postgres]]
+ [[Qdrant]]). Komunikacja synchroniczna przez REST, asynchroniczna
przez [[Event_Bus]] oparty o [[RabbitMQ]].

Zasady przecinajace warstwy:

- **Stateless services**: kazda instancja backendu moze obsluzyc kazde zadanie.
- **Graf wiedzy nad tekstem**: cala domena jest reprezentowana w Qdrancie
  jako embedowany graf pojec, a nie jako surowy markdown.
- **Decyzje = ADR**: kazde rozgalenienie architektury konczy sie notatka
  typu `decision` pod tym hubem (patrz sekcja "Decyzje architektoniczne").

## Kluczowe moduly

Kazdy modul ma swoja notatke typu `module` i jest zlinkowany z tego huba
przez pole `related` oraz bullet ponizej:

- [[Auth]] - wydawanie tokenow JWT, RBAC, federacja z OAuth providerami.
- [[Embedding_Pipeline]] - potok produkcji wektorow dla tekstu i metadanych.
- [[Feed_Service]] - personalizowany feed oparty o [[Reranker]] na wektorach.
- [[Storage_Layer]] - fasada nad [[Postgres]] (ACID) i [[Qdrant]] (ANN).

## Technologie

Technologia = pojedynczy **wybor narzedzia** ze strukturowana uzasadnieniem.
Kazda technologia jest notatka typu `technology`:

- [[Qdrant]] - baza wektorowa, silnik wyszukiwania semantycznego.
- [[RabbitMQ]] - message broker dla asynchronicznych eventow domenowych.
- [[Postgres]] - relacyjna baza danych (ACID, transakcje, migracje).
- [[Modular_Monolith]] - koncept organizacji kodu (nie mylic z mikroserwisami).

## Decyzje architektoniczne

Tabela pelni role indeksu ADR. Dopisywanie nowego wiersza =
`create_decision(...)` (narzedzie robi to automatycznie pod tym hubem).

| Data       | Decyzja                                      | Status      | Link                             |
|------------|----------------------------------------------|-------------|----------------------------------|
| 2026-03-02 | Wybor Qdrant zamiast pgvector jako baza wekt. | accepted    | [[ADR__UseQdrantOverPgvector]]   |
| 2026-03-18 | RabbitMQ zamiast Kafka dla event bus          | accepted    | [[ADR__UseRabbitMQOverKafka]]    |
| 2026-04-10 | Federacja OAuth zamiast wlasnego IdP          | accepted    | [[ADR__FederateInsteadOfOwnIdP]] |

## Powiazane notatki

- [[MOC___Architektura]] - rodzicielski MOC (wyzszy poziom abstrakcji).
- [[Infrastruktura]] - hub operacyjny (deploy, CI/CD, monitoring).
- [[Modularny_Monolit]] - concept, na ktorym opiera sie podzial na moduly.
