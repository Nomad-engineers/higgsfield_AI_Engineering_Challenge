# Unified Improvement Plan — Memory Service v9

Merged from PLAN.md + AUDIT.md. Priority-ordered, wave-based parallel execution.

---

## Status Legend

- [x] Done (Wave 1)
- [ ] Pending

---

## Wave 1 — Critical Fixes (DONE)

Parallelized across 3 agents, 0 shared files.

### 1.1 Token budget overflow [x]
- **Problem:** `estimate_tokens(text) = len(text) // 3` — rough heuristic. Markdown-heavy context could exceed `max_tokens` by 1.5-2x
- **Fix applied:**
  - Changed all budget multipliers from `* 4` to `* 3` in `format_stable_facts`, `format_relevant_memories`, `format_recent_turns`, `_fallback_recall`, `_basic_recall`
  - Added hard cap at end of `_assemble_context`: if `len(context) > max_tokens * 3 * 1.2`, truncate with `...`
- **Files:** `src/services/recall_service.py`

### 1.2 Race condition + opinion arc [x]
- **Problem:** `get_active_by_key` → `deactivate` → `create` not atomic. Concurrent `POST /turns` for same `user_id` could create duplicate memories. Also nuance relationship deactivated old memory, losing opinion arcs.
- **Fix applied:**
  - Added `for_update: bool = False` param to `get_active_by_key` — uses `SELECT ... FOR UPDATE` when `True`
  - Extraction service calls with `for_update=True` during contradiction resolution
  - Nuance branch: old memory stays active (no `deactivate_by_id` call), both active. Evolution chain preserved
- **Files:** `src/repositories/memory_repo.py`, `src/services/extraction_service.py`

### 1.3 Search schema compliance [x]
- **Problem:** `SearchResult` had `key: str | None` and `type: str | None` — not in §3 contract
- **Fix applied:** Moved `key` and `type` into `metadata: {"key": "...", "type": "..."}` in all 3 search result builders
- **Files:** `src/schemas/search.py`, `src/services/search_service.py`

### 2.1 Opinion arc preservation [x] (done as part of 1.2)
- **Problem:** Nuance deactivated old memory, losing evolution arc
- **Fix applied:** Old memory stays active. Both nuance memories co-exist. `format_stable_facts` already shows evolution: `"value (evolved from: old1; old2)"`. `format_relevant_memories` shows: `"value1 → value2 → value3"` via `supersedes` chain
- **Files:** `src/services/extraction_service.py`, `src/services/recall_service.py`

### 2.2 Multi-hop query rewrite gate [x] (done as part of 1.1)
- **Problem:** `len(query.split()) <= 4` gate blocks short multi-hop queries ("Biscuit's city?" = 2 words)
- **Fix applied:** Removed word-count gate entirely. All queries go to LLM rewrite. If LLM returns `is_multi_hop: false`, original query used
- **Files:** `src/services/recall_service.py`

### 3.1 Missing `source_turn` field [x] (done alongside 1.3)
- **Problem:** Contract §3 shows `source_turn` field but `MemoryOut` only had `source_session`
- **Fix applied:** Added `source_turn: str | None = None` to `MemoryOut`, populated from `memory.source_turn_id`
- **Files:** `src/schemas/memory.py`, `src/routers/memories.py`

---

## Wave 2 — High-Impact Improvements (DONE)

### 2.3 Cross-key contradiction threshold tuning [x]
- **Problem:** Threshold 0.80 too high for semantically related but differently-worded keys (`employer` vs `occupation`)
- **Fix applied:** Lowered to 0.70. Added `ALWAYS_CROSS_CHECK_PAIRS` whitelist: `(employer, title)`, `(employer, occupation)`, `(location, city)`, `(spouse, spouse_occupation)`. Pairs in whitelist are always checked even if similarity is below threshold.
- **Files:** `src/services/extraction_service.py`

### 2.4 Reranker index ambiguity fix [x]
- **Problem:** Current logic: `min(indices) == 0` → 0-based, else 1-based. Edge case: model returns `[0, 1, 3]` for 3 items — `min=0` → treats as 0-based, index 3 silently dropped
- **Fix applied:** Count valid vs out-of-bound indices. If majority valid → keep as-is (0-based). Otherwise shift by -1 (1-based). Applied to both `ranked_indices` and group `indices`.
- **Files:** `src/services/llm_service.py`

### 2.5 LLM service connection leak [x]
- **Problem:** `llm_service.close()` never called during shutdown. `httpx.AsyncClient` never cleaned up
- **Fix applied:** Added `await llm_service.close()` in lifespan shutdown handler after yield
- **Files:** `src/main.py`

### 2.6 Broaden hybrid search limits [x]
- **Problem:** Vector + BM25 search limited to 20 results per sub-query. For multi-hop, need both pieces to surface
- **Fix applied:** Increased both vector and BM25 limits from 20 to 30
- **Files:** `src/services/recall_service.py`

**Parallelization:** 2.3 + 2.4 + 2.5 share no files → parallel. 2.6 shares recall_service.py with nothing else in this wave → can go parallel too.

```
Wave 2 — parallel (no shared files):
  ├── Agent A: 2.3  (extraction_service.py + memory_repo.py)
  ├── Agent B: 2.4  (llm_service.py)
  ├── Agent C: 2.5  (main.py)
  └── Agent D: 2.6  (recall_service.py)
```

---

## Wave 3 — Polish & Edge Cases (DONE)

### 3.2 Extraction prompt refinement [x]
- **Problem:** Prompt misses edge cases: tool messages, implicit facts beyond pets, negative examples for false positives
- **Fix applied:**
  1. Added rule 16: tool message handling — tool outputs may contain implicit facts
  2. Added rule 17: entity extraction hints — "my husband Carlos" → extract spouse + name
  3. Added rule 18: negative examples — "Let's talk about X", "I was reading about Y", "my friend said..." are NOT facts
  4. Added new examples: "Can't eat shrimp" → allergy, "Need to pick up kids" → family, "my husband Carlos is a doctor" → spouse + occupation
- **Files:** `src/prompts/extract.py`

### 3.3 Recall context formatting for tight budgets [x]
- **Problem:** Headers and markdown waste tokens on tight budgets. At 128 tokens, "## User Profile\n- **location**:" = ~20% overhead
- **Fix applied:** When `budget_tokens < 256`, `format_stable_facts` and `format_relevant_memories` use compact format — no headers, no bold, no markdown. Stable facts: `"key: value (from old)"`. Relevant: `"[type/key] value"`. Plain newlines, no list markers.
- **Files:** `src/services/recall_service.py` (`format_stable_facts`, `format_relevant_memories`)

### 3.4 Temporal boost in recall [x]
- **Problem:** No recency boost in hybrid search. Stale facts may outrank recent corrections
- **Fix applied:** Added recency factor to RRF score in `rrf_merge`: `rrf_score * (1 + alpha * recency)` where `recency = 1 / (1 + days_since_creation)`, `alpha = 0.1` (`TEMPORAL_ALPHA`). Uses `datetime.now(timezone.utc)` for age calculation.
- **Files:** `src/services/recall_service.py` (`rrf_merge`)

### 3.5 Rule-based extraction fallback [x]
- **Problem:** If OpenAI API unavailable, extraction completely disabled
- **Fix applied:** Created `src/services/rule_extractor.py` with `RuleExtractor` class using regex patterns for location, employer, allergy, pet, occupation, name. Integrated into `extraction_service.py` as fallback when LLM extraction fails. Fallback memories go through normal pipeline (dedup, resolve, embed).
- **Files:** new `src/services/rule_extractor.py`, `src/services/extraction_service.py`

### 3.6 Memory repo ORM bypass fix [x]
- **Problem:** `vector_search`, `bm25_search`, `find_cross_key_similar`, `get_relevant_facts` construct `Memory` objects from raw SQL without `embedding` attribute. Any code accessing `embedding` on fetched memory gets `None`
- **Fix applied:** Added `embedding=None` to all 4 Memory() constructors from raw SQL rows.
- **Files:** `src/repositories/memory_repo.py`

**Parallelization:**
```
Wave 3 — parallel (no shared files):
  ├── Agent A: 3.2  (src/prompts/extract.py)
  ├── Agent B: 3.3 + 3.4  (recall_service.py — both formatting + temporal boost)
  ├── Agent C: 3.5  (new rule_extractor.py + extraction_service.py)
  └── Agent D: 3.6  (memory_repo.py)
```

---

## Wave 4 — Testing & Documentation

### 4.1 Recall fixture expansion
- Add 6-8 probe queries covering: correction handling, implicit fact extraction, tight token budget (128), multi-message turns with tool calls
- **Files:** `fixtures/conversations.json`, `tests/recall_quality/test_recall_fixture.py`

### 4.2 Missing test coverage
- Multi-hop specific test
- Tight budget test (`max_tokens=128`)
- Cross-key contradiction test
- Opinion evolution arc test
- Concurrent writes test

### 4.3 Verify `.env.example`
- Challenge §6 requires `.env.example` with all API keys from `config.py`

### 4.4 Update CHANGELOG with v9 entry
- Describe all Wave 1-3 changes
- Include before/after metrics

### 4.5 Update README
- Opinion evolution section (arc preservation)
- Recall strategy (improved noise gating, removed word gate)
- Rule-based fallback mention

**Parallelization:**
```
Wave 4 — parallel:
  ├── Agent A: 4.1 + 4.2  (tests/)
  ├── Agent B: 4.3 + 4.4  (.env.example + CHANGELOG.md)
  └── Agent C: 4.5  (README.md)
```

---

## Execution Summary

```
Wave 1 [DONE]:  1.1 + 1.2 + 1.3 + 2.1 + 2.2 + 3.1   (6 items, 3 agents parallel)
Wave 2 [DONE]:  2.3 + 2.4 + 2.5 + 2.6                 (4 items, parallel)
Wave 3 [DONE]:  3.2 + 3.3 + 3.4 + 3.5 + 3.6           (5 items, done)
Wave 4 [NEXT]:  4.1 + 4.2 + 4.3 + 4.4 + 4.5           (5 items, 3 agents parallel)
```

**Total: 20 items, 15 done, 5 remaining.**

---

## Score Impact Projection

| Category | Before Wave 1 | After Wave 1 | After All Waves |
|----------|--------------|--------------|-----------------|
| Recall Quality | 8 | 8.5 | 9.5 |
| Fact Evolution | 9 | 9.5 | 9.5 |
| Multi-hop Recall | 7.5 | 8.5 | 9.5 |
| Noise Resistance | 9 | 9 | 9.5 |
| Extraction Quality | 8 | 8 | 9.5 |
| Robustness | 9 | 9 | 9.5 |
| Contract | 9 | 9.5 | 9.5 |
| **Overall** | **8.5** | **~8.8** | **~9.4** |
