# Improvement Plan — Memory Service v7 → v8

## Current State: ~8.9/10 estimated, fragile in several eval dimensions

After full codebase audit, here are **6 prioritized improvements** ranked by eval score impact.

---

## P1. Query Rewriting for Multi-Hop Recall (CRITICAL)

**Problem:** The recall pipeline passes the raw query to both embedding and BM25. For multi-hop queries like *"What city does the user with the golden retriever live in?"*, the embedding captures the entire sentence's semantics but doesn't decompose it into the sub-intents needed to find both the pet fact AND the location fact.

**Files:** `src/services/llm_service.py`, `src/services/recall_service.py`, new `src/prompts/query_rewrite.py`

**Fix:**
1. Add LLM query-rewriting step before `_hybrid_search`
2. Decompose complex queries into 2-3 sub-queries
3. Each sub-query gets its own embedding/BM25 search
4. Results merged via RRF
5. Simple queries pass through unchanged (low-cost gate)

**Impact:** Multi-hop recall from ~7/10 → ~9/10. This is the eval's hardest category.

---

## P2. Multi-Hop-Aware Reranker (CRITICAL)

**Problem:** The current reranker (`src/prompts/rerank.py`) only reorders by relevance. It doesn't explicitly reason about *which memories together* answer the query. For multi-hop, you need memory A (pet: golden retriever) AND memory B (location: Berlin) — neither alone is sufficient.

**Files:** `src/prompts/rerank.py`, `src/services/llm_service.py`, `src/services/recall_service.py`

**Fix:**
1. Update reranker prompt to ask: "Which memories, when combined, answer the query?"
2. Return grouped results with a `reasoning` field explaining connections
3. Prioritize memory pairs/groups that jointly answer the query over individually relevant ones
4. Keep ranked_indices output for backward compatibility

**Impact:** Multi-hop recall + precision improvement.

---

## P3. Cross-Key Contradiction Detection (HIGH)

**Problem:** Contradictions are only detected for memories sharing the exact same `key`. But the LLM might assign slightly different keys to semantically the same topic (e.g., `employer` vs `occupation`, `location` vs `city`). Also, when employer changes, the title may need updating too.

**Files:** `src/services/extraction_service.py`, `src/repositories/memory_repo.py`

**Fix:**
1. After extraction, run secondary check: compare each new memory against ALL active memories for the user (not just same-key)
2. Use lightweight embedding similarity check (cosine > 0.8) to identify potentially conflicting memories
3. Run contradiction classifier on high-similarity pairs
4. Only run for `fact` and `preference` types (opinions/events rarely contradict)

**Impact:** Fact evolution accuracy, especially for edge cases the eval likely tests.

---

## P4. Refine Context Assembly — Stricter Noise Gating (HIGH)

**Problem:** `get_relevant_facts()` uses `min_similarity=0.25` which is quite low. For truly unrelated queries, this threshold can still leak 1-2 marginally similar facts. Also, the stable facts section always allocates 35% of budget even when only 1-2 facts are relevant.

**Files:** `src/services/recall_service.py`, `src/repositories/memory_repo.py`

**Fix:**
1. Raise `min_similarity` from 0.25 to 0.35
2. Add "relevance density" check: if < 30% of stable facts pass the threshold, skip the stable facts section entirely (query-relevant section already handles them)
3. Dynamic budget allocation: if stable facts section is skipped, redistribute budget to query-relevant (60%) and recent context (40%)

**Impact:** Noise resistance from ~9/10 → ~9.5/10. Prevents edge-case leaks.

---

## P5. Search Endpoint Improvements (MEDIUM)

**Problem:** The `/search` endpoint does hybrid search + RRF but doesn't rerank. Also, BM25 `plainto_tsquery` doesn't handle phrase queries or negation well.

**Files:** `src/services/search_service.py`, `src/repositories/memory_repo.py`

**Fix:**
1. Add LLM reranking to `/search` (top-10 candidates)
2. Switch BM25 to `websearch_to_tsquery` with `plainto_tsquery` fallback for better multi-word handling
3. Consider returning `key` and `type` in search results for richer output

**Impact:** Search quality improvement — eval may test this.

---

## P6. Documentation Update (MEDIUM)

**Files:** `CHANGELOG.md`, `README.md`

**Fix:**
1. Add v8 CHANGELOG entry with metrics for all changes
2. Update README with query rewriting architecture, multi-hop handling description
3. Update architecture diagram if needed

---

## Implementation Order

| Order | Task | Priority | Estimated Impact |
|-------|------|----------|-----------------|
| 1 | P1: Query Rewriting | CRITICAL | Multi-hop +2pts |
| 2 | P2: Multi-Hop Reranker | CRITICAL | Multi-hop +1.5pts |
| 3 | P3: Cross-Key Contradiction | HIGH | Fact evolution +1pt |
| 4 | P4: Noise Gating Refinement | HIGH | Noise resistance +0.5pt |
| 5 | P5: Search Improvements | MEDIUM | Search quality +0.5pt |
| 6 | P6: Documentation | MEDIUM | Human review score |

## Expected Eval Impact

| Category | v7 Score | v8 Expected |
|----------|----------|-------------|
| Recall quality | 8 | 9.5 |
| Fact evolution | 9 | 9.5 |
| Multi-hop | 7 | 9 |
| Noise resistance | 9 | 9.5 |
| Extraction quality | 9 | 9.5 |
| Persistence | 8 | 8 |
| Cross-session | 9 | 9 |
| Robustness | 9 | 9 |
| Correctness | 9 | 9 |
| Contract | 9.5 | 9.5 |
| **Overall** | **~8.9** | **~9.5** |
