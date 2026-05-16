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
│  │  MemoryService      LLMService                    │   │
│  │                                                    │   │
│  │  LLM prompts: extract, contradiction,              │   │
│  │  cross_key_contradiction, query_rewrite, rerank    │   │
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

1. **Persist turn** to PostgreSQL — short transaction, committed before any LLM calls
2. **Rule-based extraction** — always runs, no API key needed. 16 regex patterns covering location, employment, pets, allergies, diet, communication style, preferences, name, and corrections. Subject gate: only extracts from `role=user` messages. Key normalization via alias map (company→employer, city→location, job→occupation). Confidence scoring based on key specificity and value length.
3. **LLM extraction** — runs in parallel when `OPENAI_API_KEY` is set. Structured JSON output via GPT-4o-mini with type (`fact|preference|opinion|event`), normalized key, atomic value, and confidence score. Rejects passing observations and distinguishes desires from facts.
4. **Merge** — LLM wins on key conflict (richer values), rules fill gaps when LLM misses or is unavailable.
5. **Same-turn deduplication** — if multiple memories share the same key from one message, merge into one memory (preferring `fact` over `opinion`). Prevents broken supersession chains.
6. **Contradiction check** — compare ONLY against the most recent existing memory for the same key. LLM classifies: `new`, `update`, `contradiction`, `correction`, or `nuance`. Separates sentiment from fact.
7. **Supersession** — for update/contradict/correction/nuance: deactivate old memory, create new with `supersedes` chain.
8. **Batch embed** — all new memories embedded in one API call (`text-embedding-3-small`, 1536d)
9. **Cross-key contradiction check** — after embedding, each new fact/preference is compared against ALL active memories with different keys via pgvector similarity (cosine > 0.70). LLM classifies the relationship. Always-cross-check pairs (employer↔title, location↔city) bypass the similarity threshold.

The turn router uses a two-phase commit: Phase 1 persists the turn and commits immediately; Phase 2 runs extraction and commits separately. If extraction fails, the turn remains in the database — no lost data. No `SELECT FOR UPDATE` locks during extraction, eliminating deadlock risk under concurrent writes.

All Pydantic schemas use `extra="forbid"` — unknown fields in request bodies are rejected with 422, ensuring strict contract compliance.

## Recall Strategy

`POST /recall` runs a 7-stage pipeline (with BM25-only fallback when no API key):

1. **Query rewriting** — LLM decomposes multi-hop queries into 2-3 sub-queries. Simple queries pass through unchanged. Sub-query embeddings batched in one API call. (Skipped without API key.)
2. **Vector search** — pgvector HNSW cosine similarity, top-20 candidates per sub-query
3. **BM25 search** — pre-computed `tsvector` column with GIN index + `websearch_to_tsquery` (falls back to `plainto_tsquery`), top-20 candidates per sub-query
4. **Reciprocal Rank Fusion** (k=60) — merge all sub-query result sets, deduplicate by memory ID
5. **LLM reranking** — top-15 fused candidates sent to GPT-4o-mini for query-relevance ranking with multi-hop reasoning. Returns grouped memories that jointly answer the query. (Skipped without API key.)
6. **Noise gating** — adaptive similarity thresholds filter irrelevant results: 0.35 floor, 0.50 breakpoint with stricter filtering below, relevance density check for stable facts
7. **Context assembly** — format ranked memories into structured text under the token budget, with relevance gating and dynamic budget allocation

### BM25-only Fallback (no API key)

When `OPENAI_API_KEY` is not set, recall and search operate without embeddings or LLM calls:

- **`/recall`** uses BM25 keyword search to find relevant memories, assembles context with the same token budget priority (stable facts 35%, query-relevant 50%, recent context 15%). Falls back to recent memories if BM25 returns no matches.
- **`/search`** uses BM25 keyword search directly, returning scored results. Falls back to recent memories if BM25 returns no matches.
- Rule-based extraction still runs, so memories are created and stored — just without LLM enrichment.

### Token Budget Priority

When budget is tight, the assembly follows this priority:

| Priority | Allocation | Content | Rationale |
|----------|-----------|---------|-----------|
| 1 | 35% (or 0%) | Stable facts (confidence ≥ 0.8, relevant to query) | Compact, high-value, filtered by query similarity (≥ 0.35). Skipped entirely if < 30% of stable facts pass the relevance threshold — budget redistributed to query-relevant (60%) and recent context (40%). |
| 2 | 50% (or 60%) | Query-relevant memories (reranked) | The query signals what the agent needs right now. Gets 60% when stable facts are skipped. |
| 3 | 15% (or 40%) | Recent session turns | Verbose and low density, only if budget remains. Gets 40% when stable facts are skipped. |

Stable facts are filtered by relevance to the query — only facts with cosine similarity > 0.35 to the query embedding are included. An adaptive noise floor applies: if the best reranked result has similarity < 0.50, a stricter threshold (0.35) applies to all results; otherwise the threshold adapts to `max(0.35, max_sim * 0.5)`. This prevents dumping the entire user profile when the query is unrelated ("What car does the user drive?" won't include dietary restrictions). Query-relevant memories get the largest share because the recall query is the strongest signal of what the agent needs. Recent conversation context is a fallback — useful but low signal density per token.

### Multi-hop Recall

For queries like "What city does the user with the dog named Biscuit live in?", the system uses a 3-layer approach:

1. **Query rewriting** — the LLM decomposes the query into ["person has a dog named Biscuit", "where that person lives"]. Each sub-query gets its own embedding + hybrid search.
2. **Multi-hop reranking** — the reranker prompt explicitly asks which memories, *when combined*, answer the query. It returns `groups` of memories that jointly contribute, with reasoning. Grouped memories are promoted to the top of results.
3. **Broad recall net** — running multiple sub-queries through both vector and BM25 search casts a wider net, increasing the chance that both the pet fact and location fact surface in the candidate set.

No explicit graph traversal — at this scale (single user, hundreds of memories), query decomposition + LLM reranking is sufficient and avoids graph maintenance overhead.

### Session-only Recall

When `user_id` is null, recall operates on session-scoped data only: memories created in that session and recent turns from the session. No cross-session knowledge is accessed.

### Noise Resistance

Irrelevant queries don't dump the entire user profile. The context assembly uses adaptive noise gating:
- **Relevance threshold** — memories with cosine similarity < 0.35 to the query are filtered out
- **Adaptive floor** — if the best result has similarity < 0.50, the floor is raised to 0.35 for all results; otherwise it adapts to `max(0.35, max_sim * 0.5)`
- **Density gating** — if fewer than 30% of stable facts pass the relevance threshold, the entire User Profile section is skipped and its budget redistributed

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

**Dual-track extraction: rules + LLM.** Rule-based extraction (16 regex patterns, user-only gate, key normalization) always runs and works without any API key. LLM extraction adds richer values and broader coverage. LLM wins on key conflict; rules fill gaps when LLM is unavailable or misses a pattern.

**Query rewriting with cheap gate.** Queries ≤ 4 words skip the rewrite LLM call entirely. Longer queries are decomposed into sub-queries only when the LLM flags `is_multi_hop: true`. Most queries pass through unchanged.

**Embeddings on memories table.** Fewer joins = simpler queries, faster recall. Tradeoff: one embedding per memory (sufficient for atomic facts).

**Search endpoint.** `/search` includes LLM reranking (top-10 candidates after RRF fusion) when the API key is available, and returns `key` and `type` fields alongside the `content` field. BM25 uses `websearch_to_tsquery` with `plainto_tsquery` fallback for better phrase handling. Without an API key, search uses BM25-only with scored results. Callers needing the full structured memory graph should use `/users/{user_id}/memories`.

## Failure Modes

| Scenario | Behavior |
|----------|----------|
| No memories for user | `/recall` returns `{"context": "", "citations": []}` — 200 OK |
| Unrelated query | Stable facts filtered by relevance; empty context if nothing matches |
| OpenAI API down | `/turns` persists the turn, rule-based extraction still runs, returns 201. LLM extraction logged as warning. |
| Missing OPENAI_API_KEY | Service starts, `/turns` works with rule-based extraction only, `/recall` uses BM25-only fallback |
| Malformed JSON | 422 Unprocessable Entity from Pydantic validation (strict `extra="forbid"`) |
| Unicode content | PostgreSQL handles UTF-8 natively, no special handling needed |
| Oversized payload | FastAPI default body size limit applies |
| Delete nonexistent | 204 No Content (idempotent) |
| Delete turn with memories | Memory's `source_turn_id` set to NULL via ForeignKey SET NULL |
| Slow disk | Recall still works from cached HNSW indexes, slight latency increase |
| LLM rate limit | Retry up to 3 times with capped exponential backoff (max 10s wait) |
| Reranker returns 0-based indices | Auto-detected and used as-is; 1-based indices converted automatically |

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
