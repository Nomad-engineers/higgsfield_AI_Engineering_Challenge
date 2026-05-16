# Memory Service — Implementation Plan

## Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Framework | FastAPI + Python 3.12 | Async-native, Pydantic validation, auto-docs, lightweight |
| Database | PostgreSQL 16 + pgvector | Relational + vector + FTS in one container, named volume for persistence |
| ORM | SQLAlchemy 2.0 (async) | Mature async engine, pgvector support, typed mappings |
| Migrations | None — inline `CREATE TABLE IF NOT EXISTS` | Single schema version, zero migration overhead, idempotent init |
| LLM | OpenAI GPT-4o-mini | Fast, cheap, excellent at structured extraction |
| Embeddings | OpenAI `text-embedding-3-small` (1536d) | Good quality/cost ratio, well-supported |
| Full-text | PostgreSQL tsvector (built-in) | BM25-style ranking, no extra service |
| Reranking | LLM-based cross-encoder prompt | Better relevance than pure similarity scores |
| Validation | Pydantic v2 | FastAPI native, fast, JSON Schema generation |
| HTTP Client | httpx (async) | For OpenAI API calls, async-native |
| Testing | pytest + pytest-asyncio + httpx | Async test support, TestClient for integration |

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                    FastAPI App                        │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐           │
│  │  Turns   │  │  Recall  │  │  Search  │  + ...    │
│  │  Router  │  │  Router  │  │  Router  │           │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘           │
│       │              │              │                 │
│  ┌────▼──────────────▼──────────────▼──────────────┐ │
│  │                 Services Layer                    │ │
│  │                                                  │ │
│  │  ┌─────────────────┐  ┌──────────────────────┐   │ │
│  │  │  Extraction Svc │  │  Recall Engine        │   │ │
│  │  │  (LLM extract   │  │  (hybrid search +    │   │ │
│  │  │   + embed +     │  │   RRF + rerank +     │   │ │
│  │  │   contradiction)│  │   assemble)          │   │ │
│  │  └────────┬────────┘  └──────────┬───────────┘   │ │
│  │           │                       │               │ │
│  │  ┌────────▼───────────────────────▼────────────┐  │ │
│  │  │            Memory Service                    │  │ │
│  │  │  (CRUD memories, fact evolution,             │  │ │
│  │  │   contradiction detection, key normalization)│  │ │
│  │  └──────────────────┬──────────────────────────┘  │ │
│  │                     │                             │ │
│  │  ┌──────────────────▼──────────────────────────┐  │ │
│  │  │            LLM Service                       │  │ │
│  │  │  (OpenAI wrapper: extract, contradict,       │  │ │
│  │  │   rerank, query expand)                      │  │ │
│  │  └─────────────────────────────────────────────┘  │ │
│  └───────────────────────────────────────────────────┘ │
│                         │                              │
│  ┌──────────────────────▼───────────────────────────┐ │
│  │       SQLAlchemy 2.0 Async Repositories          │ │
│  │  (TurnRepo, MemoryRepo)                          │ │
│  └──────────────────────┬───────────────────────────┘ │
└─────────────────────────┼─────────────────────────────┘
                          │
              ┌───────────▼───────────┐
              │  PostgreSQL 16        │
              │  + pgvector (HNSW)    │
              │  + tsvector (GIN)     │
              │                       │
              │  Volume: pgdata       │
              └───────────────────────┘
```

**Data flow:**
- `POST /turns` → persist turn → LLM extraction → contradiction check → embed → store memories
- `POST /recall` → hybrid search (vector + BM25) → RRF fusion → LLM rerank → context assembly under token budget
- All extraction happens synchronously within the request handler (60s timeout is generous)

---

## Database Schema

**Two tables only.** Embeddings and search vectors live on the `memories` table directly — fewer joins, faster recall.

```sql
-- Auto-created on startup via CREATE TABLE IF NOT EXISTS
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS turns (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id    VARCHAR NOT NULL,
  user_id       VARCHAR,
  messages      JSONB NOT NULL,
  timestamp     TIMESTAMPTZ NOT NULL,
  metadata      JSONB DEFAULT '{}',
  created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_user ON turns(user_id);

CREATE TABLE IF NOT EXISTS memories (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         VARCHAR NOT NULL,
  type            VARCHAR(20) NOT NULL,   -- fact | preference | opinion | event
  key             VARCHAR NOT NULL,        -- normalized topic: "employer", "location", "pet"
  value           TEXT NOT NULL,           -- clear structured fact
  confidence      REAL DEFAULT 1.0,
  source_session  VARCHAR NOT NULL,
  source_turn_id  UUID REFERENCES turns(id) ON DELETE CASCADE,
  supersedes      UUID REFERENCES memories(id),
  active          BOOLEAN DEFAULT TRUE,
  embedding       vector(1536),            -- NULL until embedded
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- HNSW: better recall than IVFFlat for small datasets, no training step
CREATE INDEX IF NOT EXISTS idx_memories_embedding
  ON memories USING hnsw (embedding vector_cosine_ops);

-- GIN for tsvector full-text search
CREATE INDEX IF NOT EXISTS idx_memories_user_active ON memories(user_id, active);
CREATE INDEX IF NOT EXISTS idx_memories_key ON memories(user_id, key) WHERE active = TRUE;
```

**Why not separate embedding/search tables?**
- Fewer joins = simpler queries, faster recall
- pgvector HNSW + GIN indexes coexist fine on one table
- For single-user scale, no need for separate storage

**Why HNSW over IVFFlat?**
- No training step (IVFFlat needs `SET ivfflat.probes = 10` after some data is loaded)
- Better recall-quality at low latency for datasets < 1M rows
- Just works from the first row

---

## Extraction Pipeline

```
Raw Turn Messages
       │
       ▼
┌──────────────────────────┐
│ LLM Extraction Prompt    │  GPT-4o-mini, response_format: json_schema
│ (structured output)      │
└──────────┬───────────────┘
           │
           ▼
Structured Memories (Pydantic-validated):
  [
    { type: "fact", key: "location", value: "Lives in Berlin (moved from NYC)", confidence: 0.95 },
    { type: "fact", key: "pet", value: "Has a dog named Biscuit", confidence: 0.85 },
    { type: "preference", key: "communication_style", value: "Prefers concise answers", confidence: 0.9 }
  ]
           │
           ▼
┌──────────────────────────┐
│ Contradiction Check      │  For each extracted memory:
│ (LLM comparison)         │  1. Fetch existing active memories with same key for this user
│                          │  2. LLM judges: new | update | contradiction | correction | nuance
└──────────┬───────────────┘
           │
           ├── new       → insert, embed, index
           ├── update    → insert new, mark old active=false, set supersedes
           ├── contradict → same as update, old gets lower confidence
           ├── correction → same as update, new value includes correction note
           └── nuance    → keep both active, new has higher confidence
           │
           ▼
┌──────────────────────────┐
│ Batch Embed              │  openai.embeddings.create(input=[...])
│                          │  All new memories in one API call
└──────────────────────────┘
```

### Extraction Prompt (structured output with `json_schema`)

```python
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["fact", "preference", "opinion", "event"]
                    },
                    "key": {"type": "string", "description": "Normalized topic"},
                    "value": {"type": "string", "description": "Clear atomic fact"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1}
                },
                "required": ["type", "key", "value", "confidence"]
            }
        }
    },
    "required": ["memories"]
}
```

**System prompt rules:**
1. Extract **only** what is stated or strongly implied — never infer beyond the text
2. Each memory must be **atomic** — one fact per memory, never compound
3. Classify: `fact` (objective), `preference` (liked/disliked), `opinion` (subjective view), `event` (something that happened)
4. Normalize keys to a controlled vocabulary: `employer`, `location`, `pet`, `dietary_restriction`, `programming_language`, `communication_style`, `relationship`, `hobby`, `education`, `health`, `family`, etc.
5. Detect corrections: "actually...", "I meant...", "sorry, not X — Y" → mark with corrected value
6. Detect implicit facts: "walking Biscuit this morning" → `{ type: "fact", key: "pet", value: "Has a dog named Biscuit" }`
7. Return empty array if nothing extractable
8. Include relevant temporal context: "just moved" → note recency in value

**Few-shot examples in prompt:**

| Input | Output |
|-------|--------|
| "I just moved to Berlin from NYC last month" | `{ type: "fact", key: "location", value: "Lives in Berlin, moved from NYC (recently, ~1 month ago)", confidence: 0.95 }` |
| "I love Python but honestly TypeScript is fine for big projects" | `{ type: "preference", key: "programming_language", value: "Loves Python; thinks TypeScript is fine for large projects", confidence: 0.9 }` |
| "my 3-year-old daughter loves Frozen" | `{ type: "fact", key: "child", value: "Has a daughter (age 3)", confidence: 0.9 }` |
| "actually I meant React Native, not React" | `{ type: "correction", key: "framework", value: "Uses React Native (corrected from React)", confidence: 0.95 }` |

### Contradiction Detection Prompt

For each extracted memory, compare against existing active memories with the **same key** for this user:

```
Existing memory (key="{key}"): "{old_value}"
New extraction (key="{key}"): "{new_value}"

Classify the relationship:
- "new": unrelated or genuinely new information
- "update": new fact replaces old (e.g., changed jobs, moved cities)
- "contradiction": directly contradicts (e.g., "loves X" vs "hates X")
- "correction": explicit correction of previous statement
- "nuance": adds nuance/evolution without contradicting (e.g., opinion shift)

Return: { "relationship": "new|update|contradiction|correction|nuance", "reason": "..." }
```

**Key optimization:** Only compare memories with the same `key`. This narrows the search space and avoids spurious comparisons.

### Opinion Arcs

"I love TypeScript" → "TypeScript generics are getting annoying" → "TypeScript is fine for big projects but I'd use Python for scripts"

Strategy:
- These are **not** contradictions — they're nuance additions
- LLM classifies them as `"nuance"`
- Keep **all** active with timestamps, newest gets highest confidence
- In recall context, format as: `"Opinion on TypeScript: fine for big projects, prefers Python for scripts (evolved from: initially enthusiastic, then frustrated with generics)"`
- Full arc visible in `/users/{user_id}/memories` ordered by `created_at`

---

## Recall Pipeline

```
Query: "Where does this user live?"
       │
       ├──────────────────────────────────────────┐
       ▼                                          ▼
┌─────────────────┐                    ┌──────────────────┐
│ Vector Search   │                    │ BM25 FTS Search  │
│ pgvector HNSW   │                    │ plainto_tsquery  │
│ top-20          │                    │ top-20           │
│ (user_id match) │                    │ (user_id match)  │
└────────┬────────┘                    └────────┬─────────┘
         │                                      │
         └──────────────┬───────────────────────┘
                        ▼
              ┌───────────────────┐
              │ Reciprocal Rank   │   score = Σ 1/(k + rank_i), k=60
              │ Fusion (RRF)      │   Deduplicate by memory_id
              └────────┬──────────┘
                       │
                       ▼
              ┌───────────────────┐
              │ LLM Reranking     │   Top-15 fused candidates
              │ (GPT-4o-mini)     │   → ranked by query relevance
              │                   │   → top-K selected
              └────────┬──────────┘
                       │
                       ▼
              ┌───────────────────┐
              │ Context Assembly  │   Priority order under max_tokens:
              │                   │   1. Active stable facts (high confidence)
              │                   │   2. Query-relevant memories (reranked)
              │                   │   3. Recent session context
              └────────┬──────────┘
                       │
                       ▼
              Formatted context string + citations
```

### Why no query rewriting?

Removed from the pipeline for these reasons:
1. Each LLM call adds ~300-500ms latency
2. The eval scores recall quality, not speed — but quality comes from hybrid search + reranking, not query expansion
3. GPT-4o-mini is already good at semantic matching via embeddings
4. BM25 handles keyword-heavy queries that embeddings miss

**If recall quality is insufficient**, add query expansion as a later iteration.

### Vector Search Query

```sql
SELECT m.*,
       1 - (m.embedding <=> :query_embedding) AS similarity
FROM memories m
WHERE m.user_id = :user_id
  AND m.active = TRUE
  AND m.embedding IS NOT NULL
ORDER BY m.embedding <=> :query_embedding
LIMIT 20;
```

### BM25 Search Query

```sql
SELECT m.*,
       ts_rank_cd(
         to_tsvector('english', m.key || ' ' || m.value),
         plainto_tsquery('english', :query)
       ) AS bm25_score
FROM memories m
WHERE m.user_id = :user_id
  AND m.active = TRUE
  AND to_tsvector('english', m.key || ' ' || m.value) @@ plainto_tsquery('english', :query)
ORDER BY bm25_score DESC
LIMIT 20;
```

### Reciprocal Rank Fusion

```python
def rrf_merge(vector_results, bm25_results, k=60):
    scores = {}
    for rank, memory in enumerate(vector_results):
        scores[memory.id] = scores.get(memory.id, 0) + 1.0 / (k + rank + 1)
    for rank, memory in enumerate(bm25_results):
        scores[memory.id] = scores.get(memory.id, 0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
```

### LLM Reranking Prompt

```
Query: "{query}"

Rank these memories by relevance to the query. Return ONLY the indices in order of relevance.

1. "{memory_1_value}" [{memory_1_type}, key={memory_1_key}]
2. "{memory_2_value}" [{memory_2_type}, key={memory_2_key}]
...

Return JSON: { "ranked_indices": [3, 1, 5, 2, ...] }
```

### Context Assembly — Token Budget Logic

```python
def estimate_tokens(text: str) -> int:
    """~4 chars per token for English text with Markdown formatting."""
    return max(1, len(text) // 4)

def assemble_context(
    memories: list[Memory],       # reranked, most relevant first
    stable_facts: list[Memory],   # active facts with confidence > 0.8
    recent_turns: list[Turn],     # last 2-3 turns from current session
    max_tokens: int,
) -> tuple[str, list[Citation]]:
    budget = max_tokens
    sections = []
    citations = []

    # Phase 1: Stable facts (compact, high-value, always relevant)
    facts_budget = int(budget * 0.35)
    facts_text = format_stable_facts(stable_facts, facts_budget)
    sections.append(facts_text)
    used = estimate_tokens(facts_text)

    # Phase 2: Query-relevant memories (ranked by reranker)
    relevant_budget = int(budget * 0.50)
    relevant_text, relevant_citations = format_relevant_memories(
        memories, relevant_budget
    )
    sections.append(relevant_text)
    citations.extend(relevant_citations)
    used += estimate_tokens(relevant_text)

    # Phase 3: Recent session context (if budget allows)
    remaining = budget - used - 50  # 50 token safety margin
    if remaining > 100:
        recent_text = format_recent_turns(recent_turns, remaining)
        sections.append(recent_text)
        used += estimate_tokens(recent_text)

    return "\n\n".join(s for s in sections if s), citations
```

### Priority Logic (defended in README)

**Stable facts first (35% budget)** because:
- Compact, high-confidence, almost always relevant ("vegetarian", "allergic to shellfish")
- A frozen LLM needs user context more than conversation replay
- These are the facts that prevent the agent from asking "where do you work?" again

**Query-relevant next (50% budget)** because:
- The recall query signals what the agent needs right now
- These are the "why are we even looking this up" results
- Reranked by LLM for actual relevance, not just similarity

**Recent context last (remaining budget)** because:
- Verbose and low signal density
- Useful as fallback but not worth displacing structured knowledge
- Only included if >100 tokens of budget remain

### Multi-hop Recall

For queries like "What city does the user with the dog named Biscuit live in?":

1. **Two-stage retrieval:** The hybrid search + RRF will surface both the "pet: Biscuit" and "location" memories if they exist
2. **Reranker understands multi-hop:** The LLM reranking prompt receives the full query and all candidate memories, so it can identify that BOTH the pet fact and the location fact are needed
3. **Context assembly preserves both:** If both memories score high in reranking, both appear in the output

This is a design decision: we rely on the reranker LLM to understand multi-hop queries rather than building explicit graph traversal. For the scale of this challenge (single user, hundreds of memories), the LLM reranker is sufficient.

---

## Search Endpoint (`POST /search`)

Unlike `/recall` (formatted prose), `/search` returns structured results:

```sql
-- Same hybrid search (vector + BM25 + RRF) but:
-- 1. No context assembly — return raw memory values
-- 2. No token budget — use `limit` parameter
-- 3. Include session_id, timestamp, metadata from source turn
```

Reuses the same hybrid retrieval pipeline, just skips assembly.

---

## Error Handling & Resilience

| Scenario | Behavior |
|----------|----------|
| No memories for user | `/recall` returns `{ "context": "", "citations": [] }` — 200 OK |
| OpenAI API down | `/turns` still persists the turn, extraction logged as failed, returns 201. Memories created on next successful extraction or via retry endpoint |
| Malformed JSON body | 422 Unprocessable Entity from Pydantic validation |
| Missing required fields | 422 from Pydantic |
| Unicode oddities | PostgreSQL handles UTF-8 natively, no special handling needed |
| Oversized payload | FastAPI default body size limit (handled by framework) |
| DELETE nonexistent session/user | 204 No Content (idempotent) |
| `user_id` is null | Recall/search use session-scoped data only, skip user-level memory retrieval |

### Graceful Degradation for LLM Failures

```python
async def extract_memories(messages, user_id, session_id):
    try:
        extracted = await llm_service.extract(messages)
    except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
        logger.warning(f"LLM extraction failed: {e}, storing turn without extraction")
        return []  # Turn is persisted, memories empty
    return extracted
```

---

## Project Structure

```
memory-service/
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── README.md
├── CHANGELOG.md
├── pyproject.toml
├── alembic.ini                      # (removed — using inline schema)
│
├── app/
│   ├── __init__.py
│   ├── main.py                      # FastAPI app factory, lifespan, schema init
│   ├── config.py                    # Pydantic Settings (env vars)
│   ├── database.py                  # Async engine, session factory
│   │
│   ├── models/                      # SQLAlchemy mapped classes
│   │   ├── __init__.py
│   │   ├── turn.py
│   │   └── memory.py                # Includes embedding column
│   │
│   ├── schemas/                     # Pydantic request/response models
│   │   ├── __init__.py
│   │   ├── turn.py
│   │   ├── recall.py
│   │   ├── search.py
│   │   └── memory.py
│   │
│   ├── routers/                     # FastAPI route handlers
│   │   ├── __init__.py
│   │   ├── health.py
│   │   ├── turns.py
│   │   ├── recall.py
│   │   ├── search.py
│   │   ├── memories.py
│   │   └── cleanup.py              # DELETE sessions/{id}, DELETE users/{id}
│   │
│   ├── services/                    # Business logic
│   │   ├── __init__.py
│   │   ├── extraction_service.py   # Turn → structured memories
│   │   ├── recall_service.py       # Hybrid search + RRF + rerank + assembly
│   │   ├── memory_service.py       # CRUD, fact evolution, contradiction
│   │   ├── search_service.py       # Structured search (reuse recall pipeline)
│   │   └── llm_service.py          # OpenAI wrapper (httpx async)
│   │
│   ├── repositories/                # DB access layer
│   │   ├── __init__.py
│   │   ├── turn_repo.py
│   │   └── memory_repo.py
│   │
│   ├── prompts/                     # LLM prompt templates (static strings)
│   │   ├── __init__.py
│   │   ├── extract.py
│   │   ├── contradiction.py
│   │   └── rerank.py
│   │
│   ├── dependencies.py              # FastAPI Depends (db session, auth, services)
│   │
│   └── middleware/
│       └── error_handler.py         # Global exception → JSON error
│
├── tests/
│   ├── conftest.py                  # Fixtures: TestClient, test db
│   ├── contract/
│   │   ├── test_health.py
│   │   ├── test_turns.py
│   │   ├── test_recall.py
│   │   ├── test_search.py
│   │   ├── test_memories.py
│   │   └── test_cleanup.py
│   ├── persistence/
│   │   └── test_restart.py
│   ├── robustness/
│   │   └── test_malformed_input.py
│   └── recall_quality/
│       └── test_recall_fixture.py
│
└── fixtures/
    ├── conversations.json           # 3-5 scripted multi-turn conversations
    └── expected_facts.json          # Expected facts + probe queries + expected answers
```

---

## Implementation Order

### v0 — Project scaffold (~1.5h)

- FastAPI app factory with lifespan handler
- Dockerfile (Python 3.12-slim), docker-compose.yml (Postgres 16 + pgvector)
- Inline schema creation in lifespan (no Alembic)
- Pydantic Settings for env vars
- Health endpoint
- Smoke test: `curl http://localhost:8080/health` returns 200

**Exit criteria:** `docker compose up` boots cleanly, `/health` returns 200, schema tables exist.

### v1 — HTTP contract + basic storage (~2.5h)

- All 7 endpoints via routers + Pydantic schemas
- `POST /turns` stores raw turn, returns 201
- `POST /recall` returns last 3 turns as context (naive)
- `POST /search` returns last N memories (naive)
- `GET /users/{user_id}/memories` returns empty list
- `DELETE /sessions/{id}` and `DELETE /users/{id}` — real cleanup
- Auth middleware (optional Bearer token)
- Global error handler middleware

**Exit criteria:** All endpoints return correct status codes and shapes. Smoke test passes (except structured memories).

### v2 — Extraction pipeline (~3h)

- LLM service: OpenAI async wrapper (httpx)
- Extraction prompt with structured JSON output
- `POST /turns` runs extraction → structured memories with type, key, value, confidence
- Embed memories on extraction (batch OpenAI call)
- `/users/{user_id}/memories` returns structured data
- Graceful degradation if LLM fails

**Exit criteria:** After posting a turn, `/users/{user_id}/memories` shows typed, keyed memories with confidence scores. Not raw message text.

### v3 — Hybrid recall (~3h)

- pgvector cosine search (top-20, HNSW index)
- tsvector BM25 search (top-20, GIN index)
- Reciprocal Rank Fusion
- Context assembly with token budget (stable facts → relevant → recent)
- Citations with turn_id, score, snippet

**Exit criteria:** `/recall` returns formatted context with relevant memories, respects max_tokens. Beats naive last-N-turns on self-eval fixture.

### v4 — Fact evolution (~2h)

- Contradiction detection: compare new memories against existing by key
- LLM classifies relationship (new/update/contradict/correction/nuance)
- Supersession chain: old memory active=false, new supersedes=old_id
- Opinion arcs: multiple active memories with same key, newest highest confidence
- Context assembly shows current facts, preserves history

**Exit criteria:** "I work at Stripe" then "I just joined Notion" → `/recall` mentions Notion, `/users/{id}/memories` shows Stripe as superseded.

### v5 — LLM reranking + polish (~2h)

- LLM reranking after RRF fusion
- Tune extraction prompt based on self-eval results
- Tune context assembly formatting
- Recall quality fixture runs and reports metrics

**Exit criteria:** Self-eval recall > 70% on fixture queries.

### v6 — Tests, documentation, CHANGELOG (~2h)

- Contract tests: all 7 endpoints, shapes, status codes
- Persistence test: write → restart → recall
- Concurrent sessions test: no cross-user bleeding
- Malformed input test: bad JSON, missing fields, unicode
- Recall quality fixture (3-5 conversations + probe queries)
- README: architecture, backing store, extraction, recall, fact evolution, tradeoffs
- CHANGELOG: one entry per version with observations

**Exit criteria:** `pytest` passes, README is complete, CHANGELOG has 5+ entries.

**Total: ~16 hours (2 days of focused work)**

---

## Key Design Decisions

1. **PostgreSQL + pgvector over separate vector DB** — fewer moving parts, one container, FTS built-in, good enough for single-user scale. Qdrant/Milvus would be over-engineering.

2. **Inline schema creation over Alembic** — single schema version, zero migration overhead, idempotent `CREATE TABLE IF NOT EXISTS`. If we ever need migrations, add Alembic then.

3. **HNSW over IVFFlat** — no training step, better recall for < 1M rows, works from the first insert.

4. **Embedding column on memories table** — fewer joins, simpler queries, faster recall. The tradeoff: can't have multiple embeddings per memory, but we don't need that.

5. **Synchronous extraction within `/turns`** — simpler, no race conditions, 60s timeout is generous. No Celery, no Redis queue, no async orchestration.

6. **LLM for extraction, contradiction, and reranking** — higher quality than rule-based, acceptable latency, and GPT-4o-mini is cheap (~$0.15/1M input tokens).

7. **Key normalization** — critical for contradiction detection. "employer" vs "job" vs "workplace" must match. The extraction prompt enforces a controlled vocabulary.

8. **No query rewriting (v1)** — saves one LLM call. Hybrid search + reranking is sufficient. Add later if recall quality needs it.

9. **Opinion arcs kept active** — nuance is not contradiction. "Fine for big projects" doesn't contradict "annoying generics". Both stay active, recall shows the latest with evolution note.

10. **Graceful degradation** — if OpenAI is down, turns still persist, extraction fails silently. No data loss.

---

## Environment Variables

```env
# Required
OPENAI_API_KEY=sk-...

# Optional
MEMORY_AUTH_TOKEN=              # If set, requires Bearer auth on all endpoints
PORT=8080                       # Default 8080
DATABASE_URL=postgresql+asyncpg://postgres:postgres@db:5432/memory
OPENAI_MODEL=gpt-4o-mini
OPENAI_BASE_URL=                # Override for Azure/local Ollama
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536
LOG_LEVEL=info
```

---

## Docker Configuration

### docker-compose.yml

```yaml
version: "3.8"
services:
  db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: memory
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 5s
      retries: 5

  app:
    build: .
    ports:
      - "8080:8080"
    environment:
      DATABASE_URL: postgresql+asyncpg://postgres:postgres@db:5432/memory
      OPENAI_API_KEY: ${OPENAI_API_KEY}
    depends_on:
      db:
        condition: service_healthy
    # Retry startup if DB not quite ready
    restart: unless-stopped

volumes:
  pgdata:
```

### Dockerfile

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY app/ app/
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

---

## Testing Strategy

### Contract Tests (`tests/contract/`)

Test each endpoint for correct status codes and response shapes:
- `GET /health` → 200
- `POST /turns` with valid body → 201 + `{ "id": "..." }`
- `POST /recall` with valid body → 200 + `{ "context": "...", "citations": [...] }`
- `POST /search` with valid body → 200 + `{ "results": [...] }`
- `GET /users/{id}/memories` → 200 + `{ "memories": [...] }`
- `DELETE /sessions/{id}` → 204
- `DELETE /users/{id}` → 204

### Persistence Tests (`tests/persistence/`)

Write turns, stop containers, start containers, recall — data survives.

### Robustness Tests (`tests/robustness/`)

- Bad JSON body → 422
- Missing required fields → 422
- Unicode content → 201/200
- Empty messages array → 422
- Very long content → 201/200
- Nonexistent user memories → 200 + empty list
- Delete nonexistent session → 204

### Recall Quality Fixture (`fixtures/`)

3-5 scripted conversations with known facts, then probe queries:

```json
{
  "conversations": [
    {
      "session_id": "session-1",
      "user_id": "alice",
      "turns": [
        {
          "messages": [
            {"role": "user", "content": "I just moved to Berlin from NYC"},
            {"role": "assistant", "content": "How's Berlin treating you?"}
          ],
          "timestamp": "2025-03-15T10:00:00Z"
        },
        {
          "messages": [
            {"role": "user", "content": "I started a new job at Notion as a PM"},
            {"role": "assistant", "content": "Congratulations on the new role!"}
          ],
          "timestamp": "2025-03-15T10:05:00Z"
        }
      ]
    }
  ],
  "probe_queries": [
    {
      "query": "Where does this user live?",
      "user_id": "alice",
      "expected_facts": ["Berlin", "moved from NYC"],
      "max_tokens": 512
    },
    {
      "query": "What does this user do for work?",
      "user_id": "alice",
      "expected_facts": ["Notion", "PM"],
      "max_tokens": 512
    }
  ]
}
```

Self-eval metric: `X of Y expected facts appeared in recall context` — report as fraction.

---

## Recall Quality Improvement Checklist

Track these iteration knobs after each CHANGELOG entry:

- [ ] Extraction prompt tuning (are we extracting the right memories?)
- [ ] Key normalization accuracy (are keys consistent?)
- [ ] Contradiction detection accuracy (are updates caught?)
- [ ] Hybrid search weights (vector vs BM25 balance)
- [ ] Reranker prompt (is it ranking by true relevance?)
- [ ] Context assembly formatting (is it readable by a frozen LLM?)
- [ ] Token budget allocation (35/50/15 split — tune as needed)
