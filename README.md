# Memory Service for AI Agents

A Dockerized memory service that ingests conversation turns, extracts structured knowledge via LLM, and answers recall queries with context-aware retrieval.

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

Raw conversation turns are processed into structured memories via GPT-4o-mini with `json_schema` response format:

1. **Persist turn** to PostgreSQL
2. **LLM extraction** — structured JSON output with type (`fact|preference|opinion|event`), normalized key (controlled vocabulary: employer, location, pet, dietary_restriction, etc.), atomic value, and confidence score. Extraction prompt rejects passing observations ("flights are expensive") and distinguishes desires ("wants to learn Rust" → preference, not fact).
3. **Same-turn deduplication** — if the LLM extracts multiple memories with the same key from one message, merge into one memory (preferring `fact` over `opinion`, combining values). Prevents broken supersession chains.
4. **Contradiction check** — compare ONLY against the most recent existing memory for the same key. LLM classifies the relationship: `new`, `update`, `contradiction`, `correction`, or `nuance`. Prompt explicitly separates sentiment from fact ("loves living in Berlin" ≠ new location).
5. **Supersession** — for update/contradict/correction/nuance: deactivate old memory, create new with `supersedes` chain. History preserved via linked list.
6. **Batch embed** — all new memories embedded in one API call (`text-embedding-3-small`, 1536d)

Extraction handles: explicit facts, implicit facts ("walking Biscuit" → has a dog named Biscuit), corrections ("actually, I meant React Native"), mixed fact+sentiment statements ("moved to Berlin, loving it" → one location fact), and opinion evolution. Multi-message turns (including tool calls with `name` field) are handled — the extraction pipeline concatenates all messages and processes them as a single conversation segment.

If the OpenAI API is unavailable, the turn is still persisted and extraction fails gracefully with a warning log.

## Recall Strategy

`POST /recall` runs a 5-stage pipeline:

1. **Vector search** — pgvector HNSW cosine similarity, top-20 candidates
2. **BM25 search** — pre-computed `tsvector` column with GIN index + `plainto_tsquery`, top-20 candidates
3. **Reciprocal Rank Fusion** (k=60) — merge both result sets, deduplicate by memory ID
4. **LLM reranking** — top-15 fused candidates sent to GPT-4o-mini for query-relevance ranking
5. **Context assembly** — format ranked memories into structured text under the token budget, with relevance gating

### Token Budget Priority

When budget is tight, the assembly follows this priority:

| Priority | Allocation | Content | Rationale |
|----------|-----------|---------|-----------|
| 1 | 35% | Stable facts (confidence ≥ 0.8, relevant to query) | Compact, high-value, filtered by query similarity |
| 2 | 50% | Query-relevant memories (reranked) | The query signals what the agent needs right now |
| 3 | 15% | Recent session turns | Verbose and low density, only if budget remains |

Stable facts are filtered by relevance to the query — only facts with cosine similarity > 0.25 to the query embedding are included. This prevents dumping the entire user profile when the query is unrelated ("What car does the user drive?" won't include dietary restrictions). Query-relevant memories get the largest share because the recall query is the strongest signal of what the agent needs. Recent conversation context is a fallback — useful but low signal density per token.

### Multi-hop Recall

For queries like "What city does the user with the dog named Biscuit live in?", the system relies on the hybrid search to surface both the "pet: Biscuit" and "location" memories, and the LLM reranker to identify that both are needed to answer the query. No explicit graph traversal — at this scale (single user, hundreds of memories), the LLM reranker is sufficient.

### Session-only Recall

When `user_id` is null, recall operates on session-scoped data only: memories created in that session and recent turns from the session. No cross-session knowledge is accessed.

### Noise Resistance

Irrelevant queries don't dump the entire user profile. The context assembly gates stable facts by query relevance: only memories with cosine similarity > 0.25 to the query embedding are included in the User Profile section. If the top reranked result is irrelevant, stable facts are skipped entirely. An unrelated query like "quantum physics equations" returns empty or minimal context (<300 chars).

## Fact Evolution

| Scenario | Example | Handling |
|----------|---------|----------|
| Update | "I work at Stripe" → "I just joined Notion" | Old deactivated, new supersedes old. History preserved via chain. |
| Contradiction | "I love TypeScript" → "I hate TypeScript" | Same as update. Old deactivated. |
| Correction | "actually, React Native not React" | Same as update. New value includes correction note. |
| Nuance | "love TS" → "generics annoying" → "fine for big projects" | Old deactivated, new supersedes. Latest nuance always shown in recall. Full arc visible in `/memories` with timestamps. |

The supersession chain is fully inspectable via `GET /users/{user_id}/memories` — each memory has `supersedes` and `superseded_by` fields forming a linked list of updates.

## Tradeoffs

**Optimized for recall quality, not latency.** Each `/recall` makes 2-3 LLM calls (embedding + reranking). This adds ~500ms but significantly improves relevance over vanilla cosine-top-k.

**Synchronous extraction in `/turns`.** No Celery, no Redis queue. The 60-second eval timeout is generous. Synchronous means no race conditions — after `/turns` returns, memories are immediately queryable.

**Single extraction model.** GPT-4o-mini handles extraction, contradiction, and reranking. No fallback to rule-based extraction. If OpenAI is down, extraction fails but turns persist.

**No query rewriting.** Hybrid search (vector + BM25 + reranking) covers both semantic and keyword queries. Query rewriting would add another LLM call for marginal improvement.

**Embeddings on memories table.** Fewer joins = simpler queries, faster recall. Tradeoff: one embedding per memory (sufficient for atomic facts).

**Search endpoint content format.** `/search` returns results with a single `content` field (`"key: value"`) rather than separate `key`/`value`/`type` fields. This follows conventional search API conventions (one text blob per result) and keeps the response compact. Tradeoff: callers that need structured access must parse the content string, or use `/users/{user_id}/memories` instead, which returns fully structured memory objects with all fields. The `/search` endpoint prioritizes search relevance (hybrid vector + BM25 + RRF) and simplicity; `/memories` prioritizes structured browsing of the full memory graph.

## Failure Modes

| Scenario | Behavior |
|----------|----------|
| No memories for user | `/recall` returns `{"context": "", "citations": []}` — 200 OK |
| Unrelated query | Stable facts filtered by relevance; empty context if nothing matches |
| OpenAI API down | `/turns` persists the turn, extraction logged as warning, returns 201 |
| Missing OPENAI_API_KEY | Service starts, `/turns` works without extraction, `/recall` returns empty context |
| Malformed JSON | 422 Unprocessable Entity from Pydantic validation |
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
| `OPENAI_API_KEY` | Yes | For extraction, embeddings, and reranking |
| `MEMORY_AUTH_TOKEN` | No | If set, requires Bearer auth on all endpoints |
| `PORT` | No | Default 8080 |
| `DATABASE_URL` | No | Default uses the Docker Compose Postgres |
| `OPENAI_MODEL` | No | Default `gpt-4o-mini` |
| `OPENAI_BASE_URL` | No | Override for Azure/Ollama |
| `EMBEDDING_MODEL` | No | Default `text-embedding-3-small` |
| `LOG_LEVEL` | No | Default `info` |
