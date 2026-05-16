# Memory Service — Improvement Plan

Based on full code audit and live testing (2026-05-16).

## Current State

| Suite | Result | Notes |
|-------|--------|-------|
| Contract (16 tests) | **16/16 PASS** | All endpoints, status codes, shapes correct |
| Robustness (11 tests) | **11/11 PASS** | Malformed input, unicode, concurrent sessions |
| Extraction E2E (3 tests) | **3/3 PASS** | Structured extraction, null user_id |
| Recall Quality (12 probes) | **12/12 PASS** | All expected facts found in context |
| Smoke test | **PASS** | Health, turns, recall, memories all work |
| Edge cases | **1 FAIL** | Noise resistance (see below) |

## Automated Eval Score: ~7.5/10

| Category | Score | Reasoning |
|----------|-------|-----------|
| Recall quality | **8/10** | 12/12 probes pass on self-fixture. Risk on harder private fixtures. |
| Fact evolution | **7/10** | Supersession works for employer changes. But false positives (opinion→location) and broken opinion arcs with same-key duplicates. |
| Multi-hop recall | **7/10** | "User with golden retriever lives in Berlin" works. Relies on reranker — fragile for harder cases. |
| Noise resistance | **3/10** | Critical fail. Unrelated queries dump entire user profile. Eval will penalize heavily. |
| Extraction quality | **7/10** | Structured, typed, keyed memories. But noisy extraction (flight_cost, employment_change) and missing `name` field in tool messages. |
| Persistence | **8/10** | Named volume configured correctly. Persistence test auto-skips in Docker but the mechanism works. |
| Cross-session scoping | **9/10** | Concurrent sessions test passes, no cross-user bleed. |
| Robustness | **9/10** | 11/11 robustness tests pass. Malformed JSON, unicode, edge cases all handled. |
| Correctness | **9/10** | Synchronous extraction — after POST /turns returns, memories immediately queryable. |
| Contract compliance | **8/10** | All endpoints exist with correct status codes. Missing `name` field in message schema is a gap. |

## Human Architecture Review Score: ~8.3/10

| Category | Score | Reasoning |
|----------|-------|-----------|
| Architecture soundness | **9/10** | Clean layered design. PostgreSQL+pgvector justified for single-user scale. |
| Extraction pipeline | **8/10** | Real LLM extraction with structured output. Not message chunks. Prompt has few-shot examples. |
| Recall pipeline | **8/10** | Hybrid vector+BM25+RRF+LLM rerank. Not vanilla cosine. Thoughtful. |
| CHANGELOG quality | **9/10** | 6 entries with metrics, observations, and iteration history. Very strong. |
| Code quality | **8/10** | Clean separation of concerns. Good error handling. No unnecessary abstractions. |
| Maintainability | **8/10** | Clear structure, easy to navigate. Could extend in 6 months. |

---

## Bugs & Issues (by severity)

### CRITICAL — 2 issues

#### 1. Noise resistance failure

**Files:** `src/services/recall_service.py:241`, `src/repositories/memory_repo.py:186`

`get_stable_facts()` returns ALL active high-confidence memories for a user, regardless of query relevance. The "User Profile" section (35% budget) always dumps everything unconditionally.

**Evidence:** Query "What kind of car does this user drive?" returns 1754 chars of context with 15 citations — the entire user profile.

**Eval impact:** The challenge §9A explicitly tests: *"Queries about topics never discussed. Service should return empty context, not hallucinated memories."*

**Fix (2-3 hours):**
- In `_assemble_context`, compute relevance of the top reranked result
- If the top result's score is below a threshold (e.g., the best reranked memory is clearly irrelevant), skip the "User Profile" section entirely
- Alternative: pass the query to `get_stable_facts` and filter stable facts by minimum vector similarity to the query
- Return `{"context": "", "citations": []}` for truly irrelevant queries

#### 2. Contradiction false positive — opinion supersedes fact

**Files:** `src/services/extraction_service.py:86-103`, `src/prompts/contradiction.py`

Single user message "I just moved to Berlin from NYC last month. Loving it so far." produces TWO memories with key `location`:
- `"Lives in Berlin, moved from NYC"` (fact)
- `"Loving living in Berlin so far"` (opinion about location)

The contradiction detector classifies the opinion as **superseding** the location fact. The original fact gets marked as superseded.

**Evidence:** Smoke test shows `superseded_by` on the original location fact, and "Loving living in Berlin so far" as the primary fact in recall context.

**Eval impact:** Extraction quality and fact evolution penalties.

**Fix (1-2 hours):**
- If both memories come from the same turn, don't run contradiction detection — instead, prefer the one with `type=fact` over `type=opinion`
- Tune contradiction prompt: explicitly state "A statement about the user's sentiment toward a location is NOT an update to their location fact. 'Loving living in Berlin' is an opinion about Berlin, not a new location."
- Add examples in the contradiction prompt for sentiment-vs-fact cases

---

### HIGH — 3 issues

#### 3. Message schema missing `name` field

**File:** `src/schemas/turn.py:4-7`

The contract specifies: `{ "role":"tool", "name":"string | null", "content":"string" }`

The `Message` model only has `role` and `content`. The `name` field is silently dropped by Pydantic v2 (extra fields ignored by default).

**Eval impact:** Schema compliance issue. Tool messages with `name` field lose context.

**Fix (10 minutes):**
```python
class Message(BaseModel):
    role: str
    content: str
    name: str | None = None
```
And update extraction to include tool names when formatting messages.

#### 4. Multiple same-key extractions from single turn create broken chains

**File:** `src/services/extraction_service.py:39-51`

The LLM can extract 2+ memories with the same key from one message. The contradiction detector runs on each, potentially creating supersession chains within a single turn.

**Example:** "Loves Python for backend; TypeScript is fine for big projects" → 3 separate `programming_language` memories all claim to supersede the same original.

**Fix (1 hour):**
- Group extracted memories by key before processing
- For same-key, same-turn extractions, merge them into a single memory with combined value
- Only run contradiction detection against EXISTING memories (from previous turns), not within the same extraction batch

#### 5. Opinion arc produces 4 active memories with same key

**Evidence from live data:** After sessions 2-3, alice has 4 active `programming_language` memories:
- "Loves Python for backend work; thinks TypeScript is fine for large projects" (original)
- "Would use Python for scripts and small projects" (supersedes original)
- "Finds TypeScript generics annoying" (supersedes original)
- "Thinks TypeScript is fine for big projects" (supersedes original)

All 3 newer memories point to the same `supersedes` ID, but the original is still active (because `_resolve_memory` only deactivates the specific `best_match` memory).

**Fix (part of #4):** When processing same-key extractions, consolidate them into one coherent memory instead of creating separate entries.

---

### MEDIUM — 3 issues

#### 6. Duplicate Docker volume

`docker volume ls` shows both `higgsfield_ai_engineering_challenge_pgdata` and `memory-service_pgdata`. One is likely stale from a previous build. Not a runtime issue but could cause confusion.

**Fix:** `docker volume prune` or remove the unused one.

#### 7. Extraction noise — low-value memories

The LLM creates memories that aren't useful:
- `flight_cost: "Thinks flights to Japan are too expensive"` — not a user fact
- `employment_change: "Leaving Notion to start at Stripe"` — redundant with `employer`
- `food: "wants to focus on plant-based recipes"` — overlaps with `dietary_restriction: vegetarian`
- `birthday: "Wants to go back to Japan for their birthday"` — should be `travel` desire, not `birthday` fact

**Fix (1 hour):** Tune extraction prompt to:
- Be more selective about what constitutes a persistent memory vs. a passing statement
- Avoid creating redundant memories that overlap with existing keys
- Classify desires/preferences separately from factual statements

#### 8. Token estimation crude

`len(text) // 4` can be off by 30%+ for short texts with markdown headers. At small budgets (64 tokens), it could overflow.

**Fix (30 minutes):** Use `len(text) // 3` for markdown-heavy content, or count words * 1.3 as a better approximation.

---

### LOW — 2 issues

#### 9. Reranker index assumption

The reranker prompt numbers memories starting from 1 ("1. memory..."), and the code does `idx - 1`. If the LLM ever returns 0-based indices, it picks the wrong memory. Low probability but high impact when it happens.

**Fix:** Add validation in `llm_service.rerank` — if any index is 0, assume 0-based and skip the `-1` conversion.

#### 10. `superseded_by` only computed for active memories

`routers/memories.py:18-19` — The superseded_by map only tracks active→active links. Inactive memories show `superseded_by: null` even if they are superseded by an active memory.

**Fix:** Remove the `if m.supersedes and not m.active: pass` guard — build the map from all memories regardless of active status.

---

## Competition Assessment

### Tier: Top-10 contender, not a lock for top-3

**What's in your favor:**
- CHANGELOG is outstanding — 6 entries with genuine iteration and metrics
- Architecture is clean and defensible in a 30-minute interview
- All the hard problems are attempted: contradiction detection, hybrid recall, opinion arcs
- Code quality is high, well-organized

**What's keeping you out of top-3:**
- Noise resistance (3/10) — the biggest gap, could drop 10-15% off automated score
- Contradiction false positives pollute the memory store
- Private eval fixtures will be harder than self-test, recall could drop from 100% to 70-80%

### Highest-ROI Fix Order (total ~6 hours)

| # | Fix | Time | Impact |
|---|-----|------|--------|
| 1 | Noise resistance — relevance gating in context assembly | 2-3h | +3 points on noise resistance |
| 2 | Contradiction false positives — same-turn detection + prompt tuning | 1-2h | +2 points on extraction quality + fact evolution |
| 3 | Message schema `name` field | 10m | +1 point on contract compliance |
| 4 | Same-key extraction deduplication | 1h | +1 point on extraction quality |
| 5 | Extraction prompt tuning (less noise) | 1h | +1 point on extraction quality |

### Biggest Risks When Private Eval Runs
1. Harder multi-hop queries that the reranker can't connect
2. Extraction missing implicit facts from longer, more complex conversations
3. Noise resistance failing across multiple test scenarios
4. Opinion arc handling creating chains that confuse recall
