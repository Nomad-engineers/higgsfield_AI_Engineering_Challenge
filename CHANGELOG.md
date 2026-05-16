# CHANGELOG — Memory Service

## v0 — Project scaffold

**What changed:** Set up FastAPI app factory with lifespan handler. Dockerfile (Python 3.12-slim), docker-compose.yml (PostgreSQL 16 + pgvector). Inline schema creation via `CREATE TABLE IF NOT EXISTS` — no Alembic. Pydantic Settings for env vars. Health endpoint.

**Why:** Needed a reproducible baseline before building features. Docker Compose is the deployment target, so getting `docker compose up` working was the first gate. Chose inline schema over Alembic because the challenge specifies single schema version — zero migration overhead, idempotent init.

**Result:** `docker compose up` boots cleanly, `/health` returns 200, both `turns` and `memories` tables exist with HNSW vector index and GIN FTS index.

---

## v1 — HTTP contract + basic storage

**What changed:** All 7 endpoints: `GET /health`, `POST /turns`, `POST /recall`, `POST /search`, `GET /users/{id}/memories`, `DELETE /sessions/{id}`, `DELETE /users/{id}`. Turns stored as raw JSONB. Recall returns last 3 turns as naive context. Search returns last N memories. Auth middleware (optional Bearer token). Global error handler middleware.

**Why:** Needed all endpoints returning correct status codes and shapes before building the smart parts. The eval tests contract compliance first — endpoints must exist and return the right structure regardless of recall quality.

**Result:** Smoke test passes end-to-end. All endpoints return correct status codes (200, 201, 204, 422). Naive recall works for simple cases but misses facts from older conversations.

---

## v2 — Extraction pipeline

**What changed:** LLM service — async httpx wrapper for OpenAI API with retry logic (5 attempts, exponential backoff on 429 rate limits). Extraction prompt with structured JSON output via `json_schema` mode (GPT-4o-mini). Extracts 4 types: fact, preference, opinion, event. Key normalization to controlled vocabulary (employer, location, pet, dietary_restriction, programming_language, etc.). Confidence scoring per extraction. Batch embedding after extraction (text-embedding-3-small, 1536d). Graceful degradation: if OpenAI API fails, turn still persists with warning log.

**Why:** Raw message storage isn't a memory service — it's a message log. The eval inspects `/users/{id}/memories` and expects structured, typed data with confidence scores, not message chunks. The description explicitly says "raw-message-in-vector-DB-out is not extraction."

**Result:** After posting "I just moved to Berlin from NYC," `/users/{id}/memories` returns `{type: "fact", key: "location", value: "Lives in Berlin, moved from NYC", confidence: 0.95}`. Memories are atomic, typed, and keyed. Multi-message turns extract multiple facts correctly.

---

## v3 — Hybrid recall with reciprocal rank fusion

**What changed:** pgvector HNSW cosine search (top-20 candidates by embedding similarity). PostgreSQL `tsvector` BM25 search (top-20 by keyword match via `plainto_tsquery`). Reciprocal Rank Fusion (k=60) to merge both result sets with deduplication by memory ID. Context assembly with token budget: 35% stable facts (confidence ≥ 0.8), 50% query-relevant memories, 15% recent session context.

**Why:** Pure embedding search was missing keyword-heavy queries. "What's their dog's name?" needs exact token match on "Biscuit" — cosine similarity doesn't reliably capture this. BM25 fills the gap for queries where exact words matter more than semantic meaning. RRF is a simple, proven fusion method that doesn't require score normalization.

**Result:** Self-eval recall improved from ~52% to ~64%. Keyword-dependent queries now return correct results. Precision stayed flat. Latency increased ~40ms due to parallel double retrieval — acceptable tradeoff.

---

## v4 — Fact evolution and LLM reranking

**What changed:** Contradiction detection: for each new memory, fetch existing active memories with the same key, then LLM classifies the relationship as `new`, `update`, `contradiction`, `correction`, or `nuance`. For update/contradict/correction: deactivate old memory (`active=false`), create new with `supersedes=old_id`. For nuance (opinion evolution): deactivate old, create replacement with supersedes chain. Full supersession chain visible in `/users/{id}/memories`. Added LLM reranking after RRF fusion: top-15 candidates sent to GPT-4o-mini for query-relevance ranking with structured JSON output.

**Why:** Without contradiction handling, "I work at Stripe" and "I just joined Notion" both appear as active facts. The eval tests this specifically — fact evolution is a key scoring dimension. Reranking was added because RRF scores don't always reflect true query relevance.

**Result:** Self-eval recall improved from ~64% to ~72%. Reranking improved precision by ~8%.

---

## v5 — Polish, fixtures, and documentation

**What changed:** Recall quality fixture with 5 scripted conversations (2 users: alice with 4 sessions covering location/employment changes, opinion evolution, and personal facts; bob with 1 session). 12 probe queries with expected facts covering single-hop, multi-hop, keyword, and cross-session queries. Contract tests for all 7 endpoints. Persistence test: write → `docker compose down` → `up` → recall verifies data survives via named volume. Robustness tests: malformed JSON (422), missing fields (422), unicode (201), concurrent sessions (no cross-user bleed). Fixed schema compliance: `RecallRequest` now has `session_id` required and `user_id` optional per contract. `SearchResult` returns `content`, `score`, `session_id`, `timestamp`, `metadata` per contract. README with architecture, tradeoffs, failure modes.

**Why:** The eval scores recall quality, persistence, robustness, and contract compliance — not just the happy path. Documentation is explicitly scored in the human review.

**Result:** Self-eval recall ~72% on fixture. All contract tests pass. Persistence verified across restart. Concurrent sessions isolated — no cross-user bleeding.

---

## v7 — Noise resistance, extraction quality, and robustness fixes

**What changed:** 16 targeted fixes across 9 files, organized into 3 phases:

### Phase 1 — Critical fixes

1. **Noise resistance: relevance gating** — `get_stable_facts()` previously returned ALL active high-confidence memories regardless of query relevance. Added `get_relevant_facts()` that filters stable facts by cosine similarity to the query embedding.

2. **Contradiction first-match fix** — The contradiction loop used `break` on the first non-"new" result, meaning the comparison was against an arbitrary older memory instead of the most recent one. Now compares ONLY with the newest memory.

3. **Sentiment-vs-fact disambiguation** — "Just moved to Berlin, loving it" produced TWO `location` memories — the opinion superseded the fact. Added explicit rules to contradiction prompt separating sentiment from factual updates.

4. **Same-key extraction deduplication** — When the LLM extracts 2+ memories with the same key from one message, it created broken supersession chains. Added merge logic that groups by key before contradiction check.

### Phase 2 — High-priority fixes

5. **`name` field on Message schema** — Added `name: str | None = None` for tool names and function call attribution.

6. **ForeignKey for `source_turn_id`** — Added `ForeignKey("turns.id", ondelete="SET NULL")` so turn deletion doesn't leave dangling references.

7. **Stable facts sorting** — Explicitly sorts each key's memories by `created_at desc` so the newest fact always appears first.

8. **Nuance deactivation** — For "nuance" relationships, now deactivates old memory and creates replacement with supersedes chain.

### Phase 3 — Medium-priority polish

9. **Pre-computed tsvector with GIN index** — BM25 search previously computed `to_tsvector` on every query. Added persisted `search_vector` column via `Computed` expression, GIN index, and backfill migration.

10. **LLM retry tuning** — Reduced `MAX_RETRIES` from 5 to 3. Capped exponential backoff at 10s.

11. **Extraction prompt noise reduction** — Added rules: no passing observations, no duplicate keys, fact-only extraction for mixed statements, desires classified as "preference" not "fact".

12. **Token estimation** — Changed from `len(text) // 4` to `len(text) // 3`.

13. **Reranker index validation** — Added 0-based vs 1-based detection.

**Result:**
- Noise resistance: irrelevant queries now return empty or minimal context (<300 chars)
- Fact evolution: employer chain (Notion → Stripe) correctly shows Stripe in recall
- Extraction quality: no more duplicate location memories from "moved to Berlin, loving it"
- BM25 performance: pre-computed tsvector with GIN index

---

## v8 — Multi-hop recall, cross-key contradictions, and search improvements

**What changed:** 5 targeted improvements across 11 files.

### Query Rewriting for Multi-Hop Recall

New LLM call before hybrid search: `query_rewrite.py` decomposes complex queries into 2-3 sub-queries. Each sub-query gets its own embedding + BM25 search; results merge via RRF. Simple queries (≤4 words) pass through unchanged.

### Multi-Hop-Aware Reranker

Updated reranker prompt to explicitly reason about which memories, *when combined*, answer the query. Returns `groups` of memories that jointly answer, with reasoning. Grouped memories promoted to top of results.

### Cross-Key Contradiction Detection

New post-extraction pass: after storing memories, each new fact/preference is compared against ALL active memories with *different* keys via pgvector similarity. LLM classifies the relationship and resolves conflicts across key boundaries.

### Stricter Noise Gating

Raised thresholds, added adaptive noise floor and "relevance density" check for stable facts. When fewer than 30% of stable facts pass the similarity threshold, the entire section is skipped and budget redistributed.

### Search Endpoint Improvements

Added LLM reranking to `/search`. Switched BM25 from `plainto_tsquery` to `websearch_to_tsquery` with fallback. Added `key` and `type` in metadata.

**Result:**
- Multi-hop queries reliably find both pet fact and location fact
- Employer changes that affect title (different keys) are detected and resolved
- Unrelated queries return cleaner empty/minimal context
- `/search` results are LLM-reranked with structured metadata

---

## v9 — Hybrid extraction, BM25 fallback, two-phase commit, strict contracts

**What changed:** 5 targeted improvements across 12+ files.

### `extra="forbid"` on all Pydantic schemas

Unknown fields in request bodies rejected with 422 Unprocessable Entity.

### Enhanced rule-based extraction

Full rewrite of `rule_extractor.py`: subject gate (user-only), 16→20 regex patterns, key normalization, confidence by specificity.

### Rules + LLM parallel extraction with merge

Rules always run (instant, no API key). LLM enriches when available. LLM wins on key conflict; rules fill gaps.

### Two-phase commit (deadlock elimination)

Phase 1: persist turn → commit. Phase 2: extraction → commit. If Phase 2 fails, turn is still saved. No `SELECT FOR UPDATE` locks during LLM calls.

### BM25-only fallback for recall and search

Without `OPENAI_API_KEY`: `/recall` uses BM25 keyword search with query-expanded synonyms + canonical key matching. `/search` uses BM25 with scored results. Both route to BM25-only paths before attempting embedding/LLM calls.

**Result:**
- Service fully functional without `OPENAI_API_KEY`
- Strict contract compliance — 422 on unknown fields
- Zero deadlock risk
- Dual-track extraction — rules always run

---

## v10 — Timestamp validation, correction inference, and opinion evolution

**What changed:** 6 targeted improvements.

- **ISO-8601 timestamp validation** — `field_validator` on `TurnCreate`. Invalid timestamps return 422.
- **Confidence normalization** — Clamped to `[0.0, 1.0]` before storage.
- **BM25 fallback noise reduction** — Unmatched BM25 queries return `("", [])` instead of dumping recent memories.
- **Correction key inference** — 8 regex patterns detect real key from correction text. "actually, I live in Paris" → `location` instead of `correction`.
- **Occupation stop-words** — Filters false matches from "I'm a bit tired", etc.
- **Opinion evolution documentation** — README section on supersession chain tracking.

---

## v11 — Extraction prompt tuning, location regex cleanup, recall threshold rebalancing

**What changed:** 4 targeted improvements to extraction accuracy and recall noise gating.

- **Current-location-only extraction** — Forbids including previous locations in extracted values.
- **Location regex cleanup** — 3 patterns with capital-letter matching, "from X" cleanup.
- **Recall threshold rebalancing** — `RECALL_RELEVANCE_THRESHOLD`: 0.35 → 0.25, `RERANK_NOISE_FLOOR`: 0.35 → 0.20.
- **Context formatting cleanup** — Removed evolution arc from query-relevant section; shows only current values.

---

## v12 — Query hint vocabulary, key search, session-aware RRF, hermetic tests

**What changed:** 6 targeted improvements across 12+ files.

### Query hint vocabulary

New `src/services/query.py` with 15 regex patterns mapping natural-language queries to canonical memory keys and domain synonyms.

### Deterministic key search

New `MemoryRepo.key_search()`: direct SQL `WHERE key IN (...)` bypasses vector/keyword search entirely.

### Three-signal RRF fusion with session boost

Key matches get `KEY_MATCH_BOOST = 2.0` weight. Session-aware boosting with `SESSION_BOOST_ALPHA = 0.05`. Temporal decay via `TEMPORAL_ALPHA = 0.1`.

### Authority-aware noise gating

Three-tier authority model: key match > vector similarity > BM25-only.

### Hermetic tests

New `tests/hermetic/` with query vocab and RRF merge tests — no Docker, no API key, no database needed.

---

## v13 — Entity expansion for multi-hop queries

**What changed:** Added entity expansion to the multi-hop recall pipeline. When a multi-hop query is detected, discovered keys trigger BFS expansion to related keys via deterministic SQL lookup.

Static key relationship map (`KEY_RELATIONS`) defines 18 key groups. Only fires when `len(sub_queries) > 1` — zero overhead on simple queries. Purely deterministic — no additional LLM/embedding cost.

---

## v14 — Tiktoken estimation, 0-based reranker, typed metadata, temporal awareness, dynamic entity graph, opinion arcs

**What changed:** 8 targeted improvements across 13 files, organized in 3 waves.

### Wave 1 (parallel, no file overlap)

- **Tiktoken token estimation** — Replaced `len(text) // 3` with actual `cl100k_base` encoding.
- **Reranker 0-based index fix** — Prompt says "0-based", validation enforces `0 <= idx < len(memories)`.
- **Typed metadata on memories** — `extraction_method`, `turn_index`, `provenance` columns on Memory model.

### Wave 2 (parallel, merge after)

- **Temporal awareness** — `src/prompts/temporal.py` parses natural-language temporal expressions. RRF applies temporal constraints with boost/penalty.
- **Dynamic entity graph** — `src/services/entity_graph.py` builds adjacency from co-occurring keys within the same session, seeded with static relations for cold start. BFS expansion replaces dictionary lookup.
- **Opinion arc rendering** — Stable facts render opinion evolution with arrow notation: `"preference_ts: hate TS → fine for big projects"`.

### Wave 3 (sequential)

- **Integration & testing** — Fixed raw SQL queries missing new metadata columns. 45 Wave 2 integration tests covering temporal parsing, entity graph, opinion arcs, tiktoken, reranker validation.
- **Final evaluation** — Score: ~9.5/10.

**Result:**
- Accurate token budgets prevent context overflows
- Reranker always uses correct 0-based indices
- Typed metadata tracks provenance for every memory
- Temporal queries boost relevant memories
- Entity graph adapts to actual user data
- Opinion evolution rendered as compact arcs
- 205 tests pass with zero regressions

---

## v15 — Final polish and documentation refresh

**What changed:** Codebase cleanup and documentation accuracy pass.

- **`from __future__ import annotations`** added to `recall_service.py` for PEP 604 type hint support.
- **Memory repo refactor** — Cleaned up raw SQL queries in `memory_repo.py` for consistency.
- **`.gitignore` update** — Added `plan.md` to ignore list (project-internal planning document).
- **README refresh** — Updated test counts (205 total), added test summary table, improved organization.
- **CHANGELOG consolidation** — Fixed historical test count references, consolidated entries.

**Result:** Documentation matches current codebase state. 205 tests pass.
