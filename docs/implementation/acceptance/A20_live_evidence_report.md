# ScholarRAG

Using those results, what are the absolute and relative recall@5 gains of hybrid retrieval over the dense baseline? Show the calculation in at most 80 words and cite the source.

The absolute gain is 0.25 [C1]. The relative gain is approximately 40.3% [C1]. The calculation for relative gain is (0.87 - 0.62) / 0.62 [C1]. These values are derived from the dense baseline recall@5 of 0.62 and hybrid retrieval recall@5 of 0.87 [C2]. The evaluation used 120 fixed questions over six documents [C2]. The comparison employed identical questions and source documents for all methods [C2]. The absolute gain of 0.25 is explicitly stated in the results [C1].

- C1 · p.2: 3. Results

The absolute gain of hybrid retrieval over the dense baseline is 0.25.

The relative gain is approximately 40.3 percent: (0.87 - 0.62) / 0.62.

Reranking adds a further 0.04 absolute recall@5.

This document is created only for acceptance testing. No personal or third-party paper content is included.
- C2 · p.1: Authored test document. These are synthetic evaluation values, not claims about real research.

1. Method

A retrieval evaluation uses 120 fixed questions over six documents.

The dense baseline achieves recall@5 of 0.62.

Hybrid retrieval achieves recall@5 of 0.87.

Hybrid retrieval with reranking achieves recall@5 of 0.91.

The comparison uses identical questions and source documents for all three methods.

The aim is to test document ingestion, evidence retrieval and citations.
