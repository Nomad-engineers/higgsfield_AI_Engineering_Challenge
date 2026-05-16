# Memory Service — Full Fix & Improvement Plan

**Date:** 2026-05-16
**Status:** Planning
**Estimated total:** ~8-10 hours of focused work

---

## Phase 1: CRITICAL FIXES (must-do, blocks everything else)
## Phase 2: HIGH PRIORITY (major eval impact)
## Phase 3: MEDIUM PRIORITY (quality polish)
## Phase 4: LOW PRIORITY (if time permits)

---

## Phase 1: CRITICAL FIXES

### Task 1.1 — Noise resistance: relevance gating in context assembly
**Priority:** CRITICAL (3/10 → 9/10 on noise resistance)
**Time:** 2-3 hours
**Agent:** ML Engineer + AI Engineer Pro
**Files:** `src/services/recall_service.py`, `src/repositories/memory_repo.py`

**Problem:** `get_stable_facts()` returns ALL active high-confidence memories for a user regardless of query relevance. "What car does the user drive?" dumps the entire profile (1754 chars, 15 citations).

**Fix:**
1. In `_assemble_context`, compute relevance of the top reranked result
2. If the top reranked score is below a threshold (e.g., the best reranked memory is clearly irrelevant), skip the "User Profile" section entirely
3. Pass the query embedding to `get_stable_facts` and filter stable facts by minimum vector similarity to the query
4. If nothing is relevant, return `{"context": "", "citations": []}`

**Implementation:**
- Add `get_relevant_facts(user_id, query_embedding, min_similarity=0.3)` to `memory_repo.py` — filters stable facts by cosine similarity to query
- In `_assemble_context`: if `len(reranked) == 0` or top reranked score is too low, skip stable facts section entirely
- Add relevance threshold constant (e.g., `RECALL_RELEVANCE_THRESHOLD = 0.25`)
- For the User Profile section, only include facts that have similarity > threshold to query

---

### Task 1.2 — Contradiction detection: fix "first match wins" logic
**Priority:** CRITICAL (fact evolution eval)
**Time:** 30 min
**Agent:** AI Engineer
**Files:** `src/services/extraction_service.py:90-103`

**Problem:** Loop over existing memories breaks on first non-"new" result. Should compare with the most recent (newest) memory only.

**Fix:**
```python
# existing already sorted by created_at desc from get_active_by_key
# Compare ONLY with the newest memory
old_mem = existing[0]
try:
    result = await llm_service.check_contradiction(
        key=key, old_value=old_mem.value, new_value=value
    )
    best_relationship = result.get("relationship", "new")
    best_match = old_mem
except Exception as e:
    logger.warning(f"Contradiction check failed: {e}")
    best_relationship = "new"
```

---

### Task 1.3 — Contradiction false positives: same-turn + sentiment-vs-fact
**Priority:** CRITICAL (extraction quality + fact evolution)
**Time:** 1-2 hours
**Agent:** AI Engineer Pro
**Files:** `src/services/extraction_service.py`, `src/prompts/contradiction.py`

**Problem:** Single message "I just moved to Berlin from NYC. Loving it so far." produces TWO `location` memories — the opinion supersedes the fact.

**Fix:**
1. Group extracted memories by key before processing contradictions
2. For same-key, same-turn extractions: prefer `type=fact` over `type=opinion`, merge into one
3. Tune contradiction prompt: add explicit rule "A statement about the user's sentiment toward X is NOT an update to their X fact. 'Loving living in Berlin' is an opinion about Berlin, not a new location."
4. Add few-shot examples in the contradiction prompt for sentiment-vs-fact cases

---

### Task 1.4 — Same-key extraction deduplication within single turn
**Priority:** CRITICAL (fixes opinion arc chains)
**Time:** 1 hour
**Agent:** AI Engineer
**Files:** `src/services/extraction_service.py:39-51`

**Problem:** LLM can extract 2+ memories with same key from one message, creating broken supersession chains.

**Fix:**
1. Group raw_memories by key after extraction
2. For same-key, same-turn extractions: merge into a single memory with combined value
3. Only run contradiction detection against EXISTING memories (from previous turns), not within same batch
4. After merging, run contradiction check once per unique key against DB

---

## Phase 2: HIGH PRIORITY

### Task 2.1 — Add `name` field to Message schema
**Priority:** HIGH (contract compliance)
**Time:** 10 min
**Agent:** AI Engineer
**Files:** `src/schemas/turn.py`, `src/services/llm_service.py:43-45`

**Fix:**
```python
class Message(BaseModel):
    role: str
    content: str
    name: str | None = None
```
And update extraction formatting to include tool names.

---

### Task 2.2 — Add fact evolution test ✅ DONE
**Priority:** HIGH (eval coverage)
**Time:** 30 min
**Agent:** AI Engineer
**Files:** `tests/recall_quality/test_recall_fixture.py`

**Done:** Added 4 tests:
1. `test_fact_evolution_employer_is_current` — recall returns Stripe, NOT Notion
2. `test_fact_evolution_history_preserved` — /memories shows Notion as superseded with correct superseded_by chain
3. `test_noise_resistance_unrelated_query` — existing, preserved
4. `test_noise_resistance_context_is_minimal_for_unrelated` — unrelated query returns empty or <300 chars

---

### Task 2.3 — ForeignKey for source_turn_id with SET NULL
**Priority:** HIGH (robustness)
**Time:** 15 min
**Agent:** AI Engineer
**Files:** `src/models/memory.py:22`

**Fix:**
```python
from sqlalchemy import ForeignKey

source_turn_id: Mapped[uuid.UUID | None] = mapped_column(
    UUID(as_uuid=True), ForeignKey("turns.id", ondelete="SET NULL"), nullable=True
)
```

---

### Task 2.4 — Stable facts sorting in _group_by_key
**Priority:** HIGH (recall quality)
**Time:** 15 min
**Agent:** AI Engineer
**Files:** `src/services/recall_service.py:40-44`

**Fix:**
```python
def _group_by_key(memories: list) -> dict[str, list]:
    grouped = defaultdict(list)
    for m in memories:
        grouped[m.key].append(m)
    for key in grouped:
        grouped[key].sort(key=lambda m: m.created_at, reverse=True)
    return dict(grouped)
```

---

### Task 2.5 — Nuance memories: deactivate old when nuance supersedes
**Priority:** HIGH (duplicate recall context)
**Time:** 30 min
**Agent:** AI Engineer
**Files:** `src/services/extraction_service.py:121-133`

**Problem:** For "nuance", old memory stays active → both appear in recall, creating duplicates.

**Fix:** For nuance relationships, also deactivate the old memory (or mark it as "subsumed"). Only the newest nuance should be active. Alternatively: keep both active but in `format_stable_facts` and `format_relevant_memories`, deduplicate by key showing only the latest.

**Recommended approach:** Keep both active but ensure `_group_by_key` always shows the evolution arc correctly. When nuance is detected, the newest memory gets higher confidence and the grouping logic shows the evolution.

---

## Phase 3: MEDIUM PRIORITY

### Task 3.1 — BM25 GIN index with pre-computed tsvector
**Priority:** MEDIUM (performance)
**Time:** 30 min
**Agent:** Data AI ML Engineer
**Files:** `src/models/memory.py`, `src/repositories/memory_repo.py`

**Fix:**
```python
# In memory model:
from sqlalchemy import Computed, Index
from sqlalchemy.dialects.postgresql import TSVECTOR

search_vector = mapped_column(
    TSVECTOR,
    Computed("to_tsvector('english', coalesce(key, '') || ' ' || coalesce(value, ''))", persisted=True),
    nullable=True,
)

# In __table_args__:
Index("idx_memories_search", "search_vector", postgresql_using="gin"),
```
Update bm25_search to use `search_vector` column instead of on-the-fly `to_tsvector`.

---

### Task 3.2 — LLM retry: reduce MAX_RETRIES, cap backoff
**Priority:** MEDIUM (resilience)
**Time:** 10 min
**Agent:** AI Engineer
**Files:** `src/services/llm_service.py:11,29-38`

**Fix:**
```python
MAX_RETRIES = 3
# In _post_with_retry:
wait = min(2 ** (attempt + 1), 10)
```

---

### Task 3.3 — Extraction prompt tuning (reduce noise)
**Priority:** MEDIUM (extraction quality)
**Time:** 1 hour
**Agent:** ML Engineer
**Files:** `src/prompts/extract.py`

**Fix:** Add rules:
1. "Do NOT extract passing observations (e.g., 'flights are expensive') — only persistent user attributes"
2. "Avoid creating redundant memories that overlap with existing keys"
3. "Classify desires/wants separately from factual statements"
4. "If a statement is about the user's sentiment/opinion about a topic, and another fact about that same topic exists, do NOT extract the sentiment as a separate fact"

---

### Task 3.4 — Token estimation improvement
**Priority:** MEDIUM (recall budget accuracy)
**Time:** 15 min
**Agent:** AI Engineer
**Files:** `src/services/recall_service.py:18-19`

**Fix:**
```python
def estimate_tokens(text: str) -> int:
    # Markdown-heavy content averages ~3 chars/token
    return max(1, len(text) // 3)
```

---

### Task 3.5 — Reranker index validation (0-based guard)
**Priority:** MEDIUM (correctness)
**Time:** 10 min
**Agent:** AI Engineer
**Files:** `src/services/llm_service.py:122`

**Fix:** Validate indices — if any index is 0, assume 0-based indexing:
```python
indices = parsed.get("ranked_indices", [])
if indices and min(indices) == 0:
    return indices  # Already 0-based
return [idx - 1 for idx in indices]  # Convert from 1-based
```

---

### Task 3.6 — superseded_by map fix for inactive memories
**Priority:** MEDIUM (memory history correctness)
**Time:** 10 min
**Agent:** AI Engineer
**Files:** `src/routers/memories.py:16-19`

**Fix:** Build superseded_by map from ALL memories, not just active:
```python
superseded_by_map: dict[str, str] = {}
for m in memories:
    if m.supersedes:
        superseded_by_map[str(m.supersedes)] = str(m.id)
```

---

## Phase 4: LOW PRIORITY (if time permits)

### Task 4.1 — Document multi-hop limitation in README
**Files:** `README.md`

### Task 4.2 — Document search endpoint content format tradeoff
**Files:** `README.md`

### Task 4.3 — Update CHANGELOG.md with iteration entry for this round of fixes

---

## Execution Order (optimized for eval impact)

| Step | Task | Agent | Time | Cumulative |
|------|------|-------|------|------------|
| 1 | 1.1 Noise resistance | ML Engineer | 2-3h | 2-3h |
| 2 | 1.2 Contradiction first-match fix | AI Engineer | 30m | 3-3.5h |
| 3 | 1.3 Contradiction false positives | AI Engineer Pro | 1-2h | 4-5.5h |
| 4 | 1.4 Same-key deduplication | AI Engineer | 1h | 5-6.5h |
| 5 | 2.1 Message name field | AI Engineer | 10m | 5.2-6.7h |
| 6 | 2.4 _group_by_key sorting | AI Engineer | 15m | 5.5-7h |
| 7 | 2.3 ForeignKey SET NULL | AI Engineer | 15m | 5.7-7.2h |
| 8 | 2.5 Nuance dedup handling | AI Engineer | 30m | 6-7.5h |
| 9 | 2.2 Fact evolution test | AI Engineer | 30m | 6.5-8h |
| 10 | 3.2 LLM retry fix | AI Engineer | 10m | 6.7-8.2h |
| 11 | 3.5 Reranker validation | AI Engineer | 10m | 6.8-8.3h |
| 12 | 3.6 superseded_by fix | AI Engineer | 10m | 6.9-8.4h |
| 13 | 3.4 Token estimation | AI Engineer | 15m | 7-8.5h |
| 14 | 3.1 BM25 GIN index | Data AI ML Eng | 30m | 7.5-9h |
| 15 | 3.3 Extraction prompt tuning | ML Engineer | 1h | 8.5-10h |
| 16 | CHANGELOG + README update | AI Engineer | 30m | 9-10.5h |

## Parallelization Strategy

**Wave 1 (parallel):**
- Task 1.1 (noise resistance) — ML Engineer
- Task 1.2 (contradiction first-match) — AI Engineer
- Task 2.1 (message name field) — AI Engineer (quick win)

**Wave 2 (parallel, after wave 1):**
- Task 1.3 + 1.4 (contradiction false positives + same-key dedup) — AI Engineer Pro
- Task 2.3 + 2.4 + 2.5 (ForeignKey, sorting, nuance) — AI Engineer

**Wave 3 (parallel, after wave 2):**
- Task 2.2 (fact evolution test) — AI Engineer
- Task 3.1 (BM25 GIN index) — Data AI ML Engineer
- Task 3.3 (extraction prompt tuning) — ML Engineer

**Wave 4 (sequential):**
- Tasks 3.2, 3.4, 3.5, 3.6 (small fixes) — AI Engineer
- CHANGELOG + README update

## Expected Score Improvement

| Category | Before | After | Delta |
|----------|--------|-------|-------|
| Recall quality | 8 | 9 | +1 |
| Fact evolution | 7 | 9 | +2 |
| Multi-hop recall | 7 | 8 | +1 |
| **Noise resistance** | **3** | **9** | **+6** |
| Extraction quality | 7 | 9 | +2 |
| Persistence | 8 | 8 | 0 |
| Cross-session | 9 | 9 | 0 |
| Robustness | 9 | 9 | 0 |
| Correctness | 9 | 9.5 | +0.5 |
| Contract compliance | 8 | 9.5 | +1.5 |
| **Automated total** | **~7.5** | **~8.9** | **+1.4** |

**Overall:** From top-10 contender → top-3 contender.
