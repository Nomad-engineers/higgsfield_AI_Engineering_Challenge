# Memory Service for AI Agents

A Dockerized memory service that ingests conversation turns, extracts structured knowledge via LLM + rule-based fallback, and answers recall queries with hybrid vector/BM25 retrieval.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                      FastAPI App                          │
│                                                          │
│  /health  /turns  /recall  /search  /memories  /cleanup  │
│     │       │       │        │        │          │       │
│     └───────┴───────┴────────┴────────┴──────────┘      │
│                          │                                │
│  ┌───────────────────────▼───────────────────────────┐   │
│  │                  Services                          │   │
│  │  ExtractionService  RecallService  SearchService   │   │
│  │  MemoryService      LLMService     QueryVocab     │   │
│  │  EntityGraph (dynamic co-occurrence)              │   │
│  │                                                    │   │
│  │  LLM prompts: extract, contradiction,              │   │
│  │  cross_key_contradiction, query_rewrite, rerank    │   │
│  │  Temporal parser: 7 pattern types → date ranges   │   │
│  │  Query vocab: 15 hint patterns → key + synonyms   │   │
│  │  Entity graph: dynamic + seed for cold start      │   │
│  └───────────────────────┬───────────────────────────┘   │
│                          │                                │
│  ┌───────────────────────▼───────────────────────────┐   │
│  │            SQLAlchemy 2.0 Async Repos              │   │
│  │            TurnRepo       MemoryRepo               │   │
│  └───────────────────────┬───────────────────────────┘   │
└──────────────────────────┼───────────────────────────────┘
                           │
               ┌───────────▼───────────┐
               │  PostgreSQL 16        │
               │  + pgvector (HNSW)    │
               │  + tsvector (GIN)     │
               │  Volume: pgdata       │
               └───────────────────────┘
```

Single-process FastAPI application with two database tables (`turns`, `memories`). All extraction and recall happen synchronously within request handlers. Docker Compose runs the app + Postgres with a named volume for persistence.

## Backing Store Choice

**PostgreSQL 16 + pgvector** — relational store, vector search, and full-text search in one container.

Why not a dedicated vector DB (Qdrant, Milvus): this is a single-user service at hundreds-of-memories scale. A separate vector DB adds operational complexity for no measurable benefit. pgvector's HNSW index provides sub-millisecond vector search at this scale, and PostgreSQL's built-in `tsvector` handles BM25-style keyword ranking without extra infrastructure.

Why HNSW over IVFFlat: no training step required, better recall quality for datasets under 1M rows, works from the first insert.

## LLM Models

| Task | Model | Why |
|------|-------|-----|
| Extraction | GPT-4o-mini | Fast, cheap ($0.15/1M input), excellent at structured JSON output via `json_schema` mode. Handles implicit fact extraction and correction detection in a single pass. |
| Contradiction | GPT-4o-mini | Same model, different prompt. Keeps the stack simple — one model for all reasoning tasks. |
| Reranking | GPT-4o-mini | LLM-based cross-encoder: receives query + candidates, returns relevance-ranked indices. Better than pure similarity scores for multi-hop queries. |
| Embeddings | text-embedding-3-small (1536d) | Good quality/cost ratio. Batched per extraction — all new memories embedded in one API call. |

Total cost per turn: ~$0.0003 (extraction + contradiction + embedding). Total cost per recall: ~$0.0002 (query embedding + reranking).

## Extraction Pipeline

Raw conversation turns are processed into structured memories via a two-track extraction system (rules + LLM):

1. **Validate and persist turn** — ISO-8601 timestamp validation via Pydantic `field_validator`, then persist to PostgreSQL in a short transaction committed before any LLM calls
2. **Rule-based extraction** — always runs, no API key needed. 20 regex patterns covering location (3 patterns with capital-letter matching and "from X" cleanup), employment (3), pets (3 including implicit detection like "walking Biscuit"), allergies (1), diet (2), communication style (2), preferences (1), name (1), corrections (1 with key inference), and fallback occupation (1 with stop-word filtering). Subject gate: only extracts from `role=user` messages. Key normalization via alias map (company→employer, city→location, job→occupation). Confidence scoring based on key specificity and value length. Correction key inference: `actually, I live in Paris` maps to `location` instead of generic `correction`.
3. **LLM extraction** — runs in parallel when `OPENAI_API_KEY` is set. Structured JSON output via GPT-4o-mini with type (`fact|preference|opinion|event`), normalized key, atomic value, and confidence score (clamped to 0.0–1.0). 18-rule prompt covering: implicit facts, corrections, compound statements, temporal context (current location only, no prior values), passing observation rejection, desire vs. fact distinction, tool message extraction, named entity context, and conversational redirect filtering.
4. **Merge** — LLM wins on key conflict (richer values), rules fill gaps when LLM misses or is unavailable.
5. **Same-turn deduplication** — if multiple memories share the same key from one message, merge into one memory (preferring `fact` over `opinion`). Prevents broken supersession chains.
6. **Contradiction check** — compare ONLY against the most recent existing memory for the same key. LLM classifies: `new`, `update`, `contradiction`, `correction`, or `nuance`. Separates sentiment from fact.
7. **Supersession** — for update/contradict/correction/nuance: deactivate old memory, create new with `supersedes` chain.
8. **Batch embed** — all new memories embedded in one API call (`text-embedding-3-small`, 1536d)
9. **Cross-key contradiction check** — after embedding, each new fact/preference is compared against ALL active memories with different keys via pgvector similarity (cosine > 0.70). LLM classifies the relationship. Always-cross-check pairs (employer↔title, location↔city) bypass the similarity threshold.

The turn router uses a two-phase commit: Phase 1 persists the turn and commits immediately; Phase 2 runs extraction and commits separately. If extraction fails, the turn remains in the database — no lost data. No `SELECT FOR UPDATE` locks during extraction, eliminating deadlock risk under concurrent writes.

All Pydantic schemas use `extra="forbid"` — unknown fields in request bodies are rejected with 422, ensuring strict contract compliance.

## Recall Strategy

`POST /recall` runs a 11-stage pipeline (with BM25-only fallback when no API key):

1. **Query rewriting** — LLM decomposes multi-hop queries into 2-3 sub-queries. Simple queries pass through unchanged. Sub-query embeddings batched in one API call. (Skipped without API key.)
2. **Temporal parsing** — extracts date ranges from temporal expressions ("last month", "3 days ago", "recently", "since January"). Returns `after`/`before` constraints and a boost factor.
3. **Query hint analysis** — 15 regex patterns map the query to canonical memory keys (location, pet, employer, food_preferences, etc.) and expand with domain-specific synonyms. No LLM needed — runs on every query.
4. **Vector search** — pgvector HNSW cosine similarity, top-30 candidates per sub-query
5. **BM25 search** — expanded query (original + hint synonyms) against pre-computed `tsvector` with GIN index, top-30 candidates per sub-query
6. **Key search** — direct SQL `WHERE key IN (...)` for hint-matched canonical keys, deterministic recall boost (bypasses fuzzy search entirely)
7. **Dynamic entity expansion** — for multi-hop queries, discovered keys are expanded via a runtime entity graph built from co-occurring keys within the same session, seeded with static relationships for cold start. Expansion results are appended to key search results before fusion. Only fires when query rewriting produces multiple sub-queries.
8. **Reciprocal Rank Fusion** (k=60) — merge vector + BM25 + key + expansion results. Key matches get 2x weight (`KEY_MATCH_BOOST`). Same-session memories get a small recency advantage (`SESSION_BOOST_ALPHA=0.05`). Temporal decay favors recent memories. Temporal constraints boost/penalize memories based on date ranges.
9. **LLM reranking** — top-15 fused candidates sent to GPT-4o-mini for query-relevance ranking with multi-hop reasoning. Returns grouped memories that jointly answer the query. 0-based indices. (Skipped without API key.)
10. **Noise gating** — three authority tiers: key match (highest authority) > vector similarity > BM25-only. Similarity thresholds filter irrelevant results: 0.25 floor for query-relevant filtering, 0.20 noise floor for reranked results; stable facts skip entirely when no authoritative results survive.
11. **Context assembly** — format ranked memories into structured text under the token budget (accurately counted via tiktoken), showing only current values. Opinion/preference evolution rendered as compact arcs ("hated TS → fine for big projects") in stable facts section.

### BM25-only Fallback (no API key)

When `OPENAI_API_KEY` is not set, recall and search operate without embeddings or LLM calls:

- **`/recall`** uses BM25 keyword search with query-expanded synonyms from hint vocabulary, plus canonical key matching for deterministic recall. Assembles context with the same token budget priority (stable facts 35%, query-relevant 50%, recent context 15%). Returns empty context if both BM25 and key search return no matches — avoids dumping irrelevant recent memories.
- **`/search`** uses expanded BM25 keyword search with key matching, returning scored results. Falls back to recent memories if both BM25 and key search return no matches.
- Rule-based extraction still runs, so memories are created and stored — just without LLM enrichment.

### Token Budget Priority

When budget is tight, the assembly follows this priority:

| Priority | Allocation | Content | Rationale |
|----------|-----------|---------|-----------|
| 1 | 35% (or 0%) | Stable facts (confidence ≥ 0.8, relevant to query) | Compact, high-value, filtered by query similarity (≥ 0.30). Skipped entirely when no query-relevant results pass the similarity threshold — budget redistributed to query-relevant (60%) and recent context (40%). |
| 2 | 50% (or 60%) | Query-relevant memories (reranked) | The query signals what the agent needs right now. Gets 60% when stable facts are skipped. |
| 3 | 15% (or 40%) | Recent session turns | Verbose and low density, only if budget remains. Gets 40% when stable facts are skipped. |

Stable facts are filtered by relevance to the query — only facts with cosine similarity > 0.30 to the query embedding are included. The noise gate works in two stages: (1) query-relevant results below `RECALL_RELEVANCE_THRESHOLD` (0.30) are dropped entirely; (2) surviving results are filtered by `RERANK_NOISE_FLOOR` (0.20). When no results survive, stable facts are skipped and budget is redistributed. This prevents dumping the entire user profile when the query is unrelated ("What car does the user drive?" won't include dietary restrictions). Query-relevant memories get the largest share because the recall query is the strongest signal of what the agent needs. Recent conversation context is a fallback — useful but low signal density per token.

### Multi-hop Recall

For queries like "What city does the user with the dog named Biscuit live in?", the system uses a 4-layer approach:

1. **Query rewriting** — the LLM decomposes the query into ["person has a dog named Biscuit", "where that person lives"]. Each sub-query gets its own embedding + hybrid search.
2. **Multi-hop reranking** — the reranker prompt explicitly asks which memories, *when combined*, answer the query. It returns `groups` of memories that jointly contribute, with reasoning. Grouped memories are promoted to the top of results.
3. **Entity expansion** — after initial retrieval, all discovered memory keys are looked up in a static relationship map (`KEY_RELATIONS`). Related keys are fetched deterministically via `key_search()`. For example, a query about "the user with the golden retriever" surfaces `pet`-keyed memories; entity expansion then also fetches `location`, `name`, `spouse`, and `child` memories, ensuring the location fact is in the candidate pool before reranking.

| Source key | Related keys |
|------------|-------------|
| `pet` | location, name, spouse, child |
| `employer` | occupation, title, location, education |
| `spouse` | spouse_occupation, location, child |
| `location` | employer, pet, name, hobby |
| `education` | employer, occupation |
| `programming_language` | framework, employer |
| + 12 more keys | ... |

Entity expansion only fires for multi-hop queries (when `len(sub_queries) > 1`), avoiding unnecessary work on simple single-hop queries.

Entity expansion complements query decomposition by following dynamic key relationships from the entity graph. The graph builds adjacency from co-occurring keys within the same session, seeded with static relationships for cold start coverage. When a multi-hop query surfaces memories with key `pet`, the system automatically fetches related keys (e.g., `location`, `name`) to ensure all facts needed to answer a multi-hop query are in the candidate set before reranking.

### Session-only Recall

When `user_id` is null, recall operates on session-scoped data only: memories created in that session and recent turns from the session. No cross-session knowledge is accessed.

### Noise Resistance

Irrelevant queries don't dump the entire user profile. The context assembly uses two-stage noise gating:
- **Query-relevance threshold** — if the best result has similarity < 0.30 to the query, all results are dropped and empty context is returned
- **Noise floor** — surviving results below 0.20 similarity are filtered out
- **Stable fact gating** — when no query-relevant results pass the threshold, the entire User Profile section is skipped and its budget redistributed

An unrelated query like "quantum physics equations" returns empty or minimal context (<300 chars).

## Fact Evolution

| Scenario | Example | Handling |
|----------|---------|----------|
| Update (same key) | "I work at Stripe" → "I just joined Notion" | Old deactivated, new supersedes old. History preserved via chain. |
| Contradiction (same key) | "I love TypeScript" → "I hate TypeScript" | Same as update. Old deactivated. |
| Correction | "actually, React Native not React" | Same as update. New value includes correction note. |
| Nuance | "love TS" → "generics annoying" → "fine for big projects" | Old deactivated, new supersedes. Latest nuance always shown in recall. Full arc visible in `/memories` with timestamps. |
| Cross-key conflict | New: employer="Joined Stripe" vs Old: title="PM at Notion" | pgvector similarity (> 0.80) detects semantic relation between different keys. LLM classifies: `update` deactivates old title, `merge` combines both values, `independent` keeps both. |

Same-key contradictions compare only against the most recent memory for that key (sorted by `created_at desc`). Cross-key contradictions compare against ALL active memories with different keys, using vector similarity as a pre-filter to avoid O(n²) LLM calls.

The supersession chain is fully inspectable via `GET /users/{user_id}/memories` — each memory has `supersedes` and `superseded_by` fields forming a linked list of updates.

### Opinion Evolution

The system tracks opinion arcs via the supersession chain. In recall, only the latest stance is returned (to avoid confusion). The full arc is inspectable via `/users/{user_id}/memories` with timestamps.

This is a deliberate tradeoff: showing all opinions in recall context would consume token budget and could confuse the agent with contradictory statements. Example: "love TS" → "generics annoying" → "fine for big projects" — recall returns only the latest stance, while `/memories` shows the full evolution.

## Tradeoffs

**Optimized for recall quality, not latency.** Each `/recall` makes 3-4 LLM calls (query rewriting + embedding + reranking + optional cross-key check). This adds ~600ms but significantly improves relevance, especially for multi-hop queries.

**Two-phase commit in `/turns`.** No Celery, no Redis queue. Phase 1 persists the turn and commits immediately. Phase 2 runs extraction (rules + LLM) and commits separately. After `/turns` returns, the turn is always persisted; memories may be missing if extraction fails but no data is lost. Two separate transactions eliminate deadlock risk from holding `SELECT FOR UPDATE` locks during network I/O.

**Dual-track extraction: rules + LLM.** Rule-based extraction (20 regex patterns, user-only gate, key normalization, correction key inference, occupation stop-words) always runs and works without any API key. LLM extraction adds richer values and broader coverage. LLM wins on key conflict; rules fill gaps when LLM is unavailable or misses a pattern.

**Query rewriting with cheap gate.** Queries ≤ 4 words skip the rewrite LLM call entirely. Longer queries are decomposed into sub-queries only when the LLM flags `is_multi_hop: true`. Most queries pass through unchanged.

**Entity expansion for multi-hop queries.** A dynamic entity graph builds adjacency from co-occurring keys within the same session, seeded with static relationships for cold start coverage. When a multi-hop query is detected, discovered keys trigger BFS expansion to related keys via deterministic SQL lookup — no embedding or LLM cost. The graph adapts as more conversations are ingested.

**Query hint vocabulary.** 15 regex patterns map natural-language queries to canonical memory keys and expand with domain synonyms — no LLM needed. "What's their dog's name?" matches the `pet` key and adds "dog", "cat", "animal", "pet name", "breed" to the BM25 query. Key search provides deterministic SQL matching that bypasses fuzzy vector/keyword search entirely. This is the third retrieval signal alongside vector and BM25.

**Session-aware RRF.** The fusion function applies a small boost (`SESSION_BOOST_ALPHA=0.05`) to memories from the active session, and temporal decay (`TEMPORAL_ALPHA=0.1`) favoring recent memories. Key matches get 2x weight in fusion to ensure deterministic hits always surface above fuzzy matches.

**Embeddings on memories table.** Fewer joins = simpler queries, faster recall. Tradeoff: one embedding per memory (sufficient for atomic facts).

**Search endpoint.** `/search` includes LLM reranking (top-10 candidates after RRF fusion) when the API key is available, and returns `key` and `type` fields alongside the `content` field. BM25 uses `websearch_to_tsquery` with `plainto_tsquery` fallback for better phrase handling. Query hint vocabulary expands BM25 queries and key search provides deterministic matches. Without an API key, search uses BM25-only with query expansion and key matching. Callers needing the full structured memory graph should use `/users/{user_id}/memories`.

## Failure Modes

| Scenario | Behavior |
|----------|----------|
| No memories for user | `/recall` returns `{"context": "", "citations": []}` — 200 OK |
| Unrelated query | Stable facts filtered by relevance; empty context if nothing matches |
| OpenAI API down | `/turns` persists the turn, rule-based extraction still runs, returns 201. LLM extraction logged as warning. |
| Missing OPENAI_API_KEY | Service starts, `/turns` works with rule-based extraction only, `/recall` uses BM25-only fallback |
| Malformed JSON | 422 Unprocessable Entity from Pydantic validation (strict `extra="forbid"`) |
| Invalid timestamp | 422 Unprocessable Entity — ISO-8601 validation via `field_validator` |
| Unicode content | PostgreSQL handles UTF-8 natively, no special handling needed |
| Oversized payload | FastAPI default body size limit applies |
| Delete nonexistent | 204 No Content (idempotent) |
| Delete turn with memories | Memory's `source_turn_id` set to NULL via ForeignKey SET NULL |
| Slow disk | Recall still works from cached HNSW indexes, slight latency increase |
| LLM rate limit | Retry up to 3 times with capped exponential backoff (max 10s wait) |
| Reranker returns 0-based indices | Validated `0 <= idx < len(memories)`, out-of-range filtered |

## How to Run

```bash
# Clone and configure
git clone <repo> memory-service && cd memory-service
cp .env.example .env
# Edit .env and add your OPENAI_API_KEY

# Start
docker compose up -d

# Wait for health
until curl -sf http://localhost:8080/health; do sleep 1; done
```

## How to Run Tests

Contract, robustness, recall quality, and extraction tests run inside the Docker container:

```bash
# Contract tests (endpoint shapes, status codes)
docker compose exec app python -m pytest tests/contract/ -v

# Recall quality (5 conversations, 12 probe queries)
docker compose exec app python -m pytest tests/recall_quality/ -v -s

# Robustness (malformed input, unicode, concurrent sessions)
docker compose exec app python -m pytest tests/robustness/ -v

# Extraction E2E (full pipeline: post turn -> extraction -> memories)
docker compose exec app python -m pytest tests/test_extraction_e2e.py -v

# All container-based tests
docker compose exec app python -m pytest tests/contract/ tests/robustness/ tests/recall_quality/ tests/test_extraction_e2e.py -v
```

Hermetic tests (no Docker, no API key, no database — pure unit tests):

```bash
# Query vocabulary and RRF merge logic
python -m pytest tests/hermetic/ -v
```

End-to-end smoke test (requires running Docker stack):

```bash
bash test_comprehensive.sh
```

The persistence test must run from the **host** (it restarts the Docker stack, so it can't run inside the container):

```bash
# Requires pytest + httpx on the host
pip install pytest httpx
pytest tests/persistence/ -v
```

## Required API Keys

Set in `.env`:

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENAI_API_KEY` | No | For LLM extraction, embeddings, and reranking. Without it, rule-based extraction and BM25 recall work. |
| `MEMORY_AUTH_TOKEN` | No | If set, requires Bearer auth on all endpoints |
| `PORT` | No | Default 8080 |
| `DATABASE_URL` | No | Default uses the Docker Compose Postgres |
| `OPENAI_MODEL` | No | Default `gpt-4o-mini` |
| `OPENAI_BASE_URL` | No | Override for Azure/Ollama |
| `EMBEDDING_MODEL` | No | Default `text-embedding-3-small` |
| `LOG_LEVEL` | No | Default `info` |
