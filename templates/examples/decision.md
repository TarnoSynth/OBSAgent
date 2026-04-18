---
tags:
  - decision
  - architecture
  - database
type: decision
parent: "[[Architektura_systemu]]"
related:
  - "[[Qdrant]]"
  - "[[pgvector]]"
  - "[[Embedding_Pipeline]]"
status: accepted
created: 2026-03-02
updated: 2026-03-02
---

# ADR: Uzycie Qdrant zamiast pgvector jako bazy wektorowej

**Podsumowanie:** Wybralismy [[Qdrant]] jako primary vector store zamiast
[[pgvector]]. Powod: wydajnosc HNSW na skali >1M wektorow oraz niezalezne
skalowanie od [[Postgres]].

## Kontekst

AthleteStack potrzebuje wyszukiwania semantycznego po **grafie pojec
domenowych** (sportowcy, tresci, treningi). Szacowana wielkosc poczatkowa
- 2M wektorow, rosnace o ~50k/miesiac. Query pattern: top-K z filtrowaniem
po tagach (tenant_id, content_type). W [[Postgres]] trzymamy juz
relacyjne dane, wiec pokusa "wszystko w jednym silniku" (przez
[[pgvector]]) byla duza.

## Decyzja

Wdrazamy [[Qdrant]] jako **osobny serwis** za [[Embedding_Pipeline]].
[[Postgres]] zostaje **source-of-truth** dla metadanych (id, timestamps,
ownership); Qdrant trzyma wylacznie wektory + payload (tenant_id, tags).
Sync: `post_save` signal -> RabbitMQ -> worker -> Qdrant upsert.

## Uzasadnienie

1. **Wydajnosc HNSW.** Benchmarki (1M wektorow, 768-dim) pokazuja Qdrant
   latency p95 ~8ms vs pgvector ~140ms przy recall@10=0.95. Roznica
   15x przy naszej skali.
2. **Niezalezne skalowanie.** Qdrant mozemy skalowac horyzontalnie bez
   dotykania [[Postgres]] (ktory ma inny profil I/O).
3. **Filtry na metadanych.** Qdrant filter API jest bogatsze (range,
   nested, geo) niz WHERE w pgvector. Nasz feed uzywa filtrow `tenant_id`
   + `content_type` w kazdym query.
4. **Dojrzalosc projektu.** Qdrant w produkcji u ~duzych playeroW
   (HuggingFace, Deepset) z 2022; pgvector dojrzalosciowo nadrabia
   ale brakuje mu HNSW native (jest IVFFlat).

## Konsekwencje pozytywne

- Latency feedu spada z 500ms do <100ms przy tej samej jakosci.
- Zmiany w [[Embedding_Pipeline]] nie obciazaja DB transakcyjnej.
- Team `ml` moze iterowac na swoim infra niezaleznie od `backend`.

## Konsekwencje negatywne

- Dodatkowy serwis w stacku operacyjnym (monitoring, backupy, HA).
- **Eventual consistency** miedzy Postgres a Qdrantem (delay ~1-3s
  na upsert). Dla UX akceptowalne, ale wymagane w teamowym briefie.
- Koszt Docker Compose i DevOps rosnie o jeden kontener.

## Migracja

Faza 1 (sprint 2026-03-W1): wdrozenie Qdranta rownolegle z pgvector,
dual-write przez [[Embedding_Pipeline]].

Faza 2 (2026-03-W2): migracja query path feedu na Qdrant (feature flag).

Faza 3 (2026-03-W3): dual-read wylaczony, pgvector jako fallback tylko
na disaster recovery.

Faza 4 (2026-04): calkowite usuniecie tabeli pgvector z [[Postgres]].

## Powiazane notatki

- [[Qdrant]] - technologia wybrana.
- [[pgvector]] - alternatywa odrzucona (notatka typu technology).
- [[Embedding_Pipeline]] - modul, ktory najbardziej zmieni sie po tej decyzji.
