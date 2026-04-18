---
tags:
  - module
  - pipeline
type: module
parent: "[[Architektura_systemu]]"
related:
  - "[[Qdrant]]"
  - "[[RabbitMQ]]"
  - "[[Postgres]]"
status: active
created: 2026-03-10
updated: 2026-04-18
---

# Embedding Pipeline

Odpowiada za produkcje i utrzymanie wektorow domenowych w [[Qdrant]].
Zalezy od [[Postgres]] (source of truth) i [[RabbitMQ]] (triggery).

## Odpowiedzialnosc

- Nasluchuje eventow `entity.updated` na [[RabbitMQ]] i kolejkuje zadania
  embedowania (bez blokowania producentow).
- Laczy kilka zrodel tekstu (nazwa, opis, tagi) w jeden embedowalny string.
- Woluje [[Model_embeddingowy]] (self-hosted BGE-large) i zapisuje wektor
  do [[Qdrant]] pod tenant-scoped kolekcja.
- NIE zarzadza lifecycle kolekcji Qdranta (to robi [[Vector_Migrator]]).
- NIE produkuje embeddingow tresci user-generated (osobny pipeline w [[Feed_Service]]).

## Kluczowe elementy

| Element              | Opis                                                           |
|----------------------|----------------------------------------------------------------|
| ``EmbeddingWorker``  | Konsument kolejki RabbitMQ; loop z backpressure i retry.       |
| ``TextAssembler``    | Sklada finalny string z pol entity (per entity_type shape).    |
| ``EmbedderClient``   | Fasada nad modelem (BGE-large); batchuje do 32 textow per call. |
| ``QdrantSyncer``     | Transakcyjny upsert wektor + payload; rollback przy bledzie.   |
| ``embed_entity()``   | Publiczne API dla consumerow synchronicznych (rzadko uzywane).  |

## Zaleznosci

- **Uzywa:** [[Qdrant]], [[RabbitMQ]], [[Postgres]], [[Model_embeddingowy]].
- **Jest uzywany przez:** [[Feed_Service]], [[Search_Service]],
  [[Admin_Panel]] (manual reindex trigger).

## Kontrakty / API

- ``embed_entity(entity_id: UUID, entity_type: EntityType) -> EmbeddingResult``
  - sync API, zwraca `dim=768` vector + upserted_at timestamp.
- Event contract:
  - Input: `entity.updated { id: UUID, type: str, tenant_id: UUID }` na kolejce `embeddings.in`.
  - Output: `embedding.synced { entity_id: UUID, vector_id: UUID }` na `embeddings.out`.
- Qdrant collection naming: `{tenant_id}__{entity_type}__v{schema_version}`.

## Decyzje architektoniczne

- [[ADR__UseQdrantOverPgvector]] - dlaczego Qdrant, nie pgvector.
- [[ADR__AsyncEmbedPipeline]] - dlaczego async przez [[RabbitMQ]], nie sync w request.
- [[ADR__BgeLargeOverOpenAI]] - dlaczego self-host embedding model.
