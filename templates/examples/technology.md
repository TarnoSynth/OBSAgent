---
tags:
  - technology
  - database
  - vector
type: technology
parent: "[[Architektura_systemu]]"
related:
  - "[[Embedding_Pipeline]]"
  - "[[Reranker]]"
status: active
role: baza wektorowa (ANN search)
created: 2026-03-02
updated: 2026-04-18
---

# Qdrant

**Rola:** baza wektorowa (ANN search).

## Do czego uzywamy

Qdrant przechowuje embeddingi **pojec domenowych** (sportowcy, druzyny,
treningi, sprzet) oraz embeddingi **tresci** (artykuly, posty, komentarze).
Kazde pojecie ma kolekcje wektorow pod dedykowanym `collection_name`.
Query: `client.search(collection, vector, limit=K, filter=...)` zwraca
K najblizszych wektorow z metadanymi.

Integracja:

- [[Embedding_Pipeline]] publikuje wektory do Qdranta po kazdej modyfikacji
  entity w [[Postgres]] (przez `post_save` signal + [[RabbitMQ]]).
- [[Reranker]] odpytuje Qdranta w pierwszym etapie (K=100), potem
  cross-encoder sortuje finalowa dziesiatke.
- [[Feed_Service]] konsumuje Reranker + personalizacje usera.

## Alternatywy odrzucone

| Alternatywa   | Dlaczego odrzucona                                           |
|---------------|--------------------------------------------------------------|
| [[pgvector]]  | Slaba wydajnosc przy >1M wektorow (brak HNSW native).        |
| [[Weaviate]]  | Zmarnowana zlozonosc dla naszego skalowania (dodatkowy schema layer). |
| [[Pinecone]]  | Vendor lock-in + koszty egress. Chcemy self-host.            |

## Linki

- [Dokumentacja Qdrant](https://qdrant.tech/documentation/) - oficjalna.
- [Benchmark HNSW vs IVFPQ](https://qdrant.tech/benchmarks/) - dlaczego HNSW.
- [[ADR__UseQdrantOverPgvector]] - pelne uzasadnienie decyzji.

## Powiazane notatki

- [[Embedding_Pipeline]] - modul produkujacy wektory.
- [[Reranker]] - konsument Qdranta w feedzie.
- [[ADR__UseQdrantOverPgvector]] - decyzja ktora wprowadzila ta technologie.
