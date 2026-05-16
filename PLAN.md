# Memory Service — NestJS Implementation Plan

## Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Framework | NestJS + TypeScript | Typed, modular, DI out of the box, user preference |
| Database | PostgreSQL 16 + pgvector | One container, relational + vector search in one place |
| ORM | TypeORM | Mature, good pgvector support via `pgvector/pgvector-node` |
| LLM | OpenAI GPT-4o-mini | Fast, cheap, good at structured extraction |
| Embeddings | OpenAI `text-embedding-3-small` | 1536 dims, good quality/cost ratio |
| Full-text | PostgreSQL tsvector (built-in) | No extra service needed, BM25-style ranking |
| Reranking | LLM-based cross-encoder prompt | Better than pure vector similarity |
| Validation | class-validator + class-transformer | NestJS native |

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                   NestJS App                     │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ Turns     │  │ Recall   │  │ Search        │  │
│  │ Controller│  │Controller│  │ Controller    │  │
│  └────┬──────┘  └────┬─────┘  └──────┬────────┘  │
│       │              │               │           │
│  ┌────▼──────────────▼───────────────▼────────┐  │
│  │              Services Layer                 │  │
│  │                                             │  │
│  │  ┌─────────────┐  ┌──────────────────────┐  │  │
│  │  │ Extraction  │  │ Recall Engine        │  │  │
│  │  │ Service     │  │ (hybrid search +     │  │  │
│  │  │             │  │  rerank + assemble)  │  │  │
│  │  └──────┬──────┘  └──────────┬───────────┘  │  │
│  │         │                    │              │  │
│  │  ┌──────▼────────────────────▼───────────┐  │  │
│  │  │         Memory Service                 │  │  │
│  │  │  (CRUD memories, fact evolution,       │  │  │
│  │  │   contradiction detection)             │  │  │
│  │  └──────────────┬────────────────────────┘  │  │
│  │                 │                            │  │
│  │  ┌──────────────▼────────────────────────┐  │  │
│  │  │         LLM Service                   │  │  │
│  │  │  (OpenAI wrapper for extraction,      │  │  │
│  │  │   contradiction check, query rewrite) │  │  │
│  │  └───────────────────────────────────────┘  │  │
│  └─────────────────────────────────────────────┘  │
│                     │                             │
│  ┌──────────────────▼──────────────────────────┐  │
│  │         TypeORM Repositories                │  │
│  │  (TurnRepo, MemoryRepo, EmbeddingRepo)      │  │
│  └──────────────────┬──────────────────────────┘  │
└─────────────────────┼────────────────────────────┘
                      │
          ┌───────────▼───────────┐
          │  PostgreSQL 16        │
          │  + pgvector extension │
          │  + tsvector FTS       │
          │                       │
          │  Volume: pgdata       │
          └───────────────────────┘
```

---

## Database Schema

### Table: `turns`
```sql
CREATE TABLE turns (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id    VARCHAR NOT NULL,
  user_id       VARCHAR,
  messages      JSONB NOT NULL,        -- array of {role, content, name?}
  timestamp     TIMESTAMPTZ NOT NULL,
  metadata      JSONB DEFAULT '{}',
  created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_turns_session ON turns(session_id);
CREATE INDEX idx_turns_user ON turns(user_id);
```

### Table: `memories`
```sql
CREATE TABLE memories (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         VARCHAR NOT NULL,
  type            VARCHAR(20) NOT NULL,  -- fact | preference | opinion | event
  key             VARCHAR NOT NULL,       -- normalized topic: "employer", "location", "pet"
  value           TEXT NOT NULL,          -- structured fact text
  confidence      FLOAT DEFAULT 1.0,
  source_session  VARCHAR NOT NULL,
  source_turn_id  UUID REFERENCES turns(id),
  supersedes      UUID REFERENCES memories(id),
  active          BOOLEAN DEFAULT TRUE,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_memories_user_active ON memories(user_id, active);
CREATE INDEX idx_memories_user_type ON memories(user_id, type);
```

### Table: `memory_embeddings`
```sql
CREATE TABLE memory_embeddings (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  memory_id   UUID REFERENCES memories(id) ON DELETE CASCADE,
  embedding   vector(1536) NOT NULL,
  content     TEXT NOT NULL,             -- text that was embedded
  created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_embeddings_vector ON memory_embeddings
  USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

### Table: `memory_search_text`
```sql
CREATE TABLE memory_search_text (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  memory_id   UUID REFERENCES memories(id) ON DELETE CASCADE,
  content     TEXT NOT NULL,
  search_vec  TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);
CREATE INDEX idx_search_vec ON memory_search_text USING gin(search_vec);
```

---

## Extraction Pipeline

### How it works

```
Raw Turn Messages
       │
       ▼
┌──────────────────┐
│ LLM Extraction   │  ← GPT-4o-mini with structured output
│ Prompt           │
└──────┬───────────┘
       │
       ▼
Structured Memories:
  [
    { type: "fact", key: "location", value: "Lives in Berlin (moved from NYC)" },
    { type: "fact", key: "pet", value: "Has a dog named Biscuit" },
    { type: "preference", key: "communication_style", value: "Prefers concise answers" }
  ]
       │
       ▼
┌──────────────────┐
│ Contradiction    │  ← Compare extracted keys against existing active memories
│ Detection        │     LLM judges: is this an update/contradiction/new info?
└──────┬───────────┘
       │
       ├── New memory → insert, embed, index
       ├── Update     → insert new, mark old as superseded
       └── Duplicate  → skip (or boost confidence)
```

### Extraction Prompt Design

System prompt instructs the LLM to:
1. Extract **only** what is stated or strongly implied
2. Classify each extraction: `fact | preference | opinion | event`
3. Normalize keys: employment → `employer`, city → `location`
4. Detect corrections: "actually...", "I meant...", "sorry, not X — Y"
5. Return empty array if nothing extractable

### Implicit Fact Extraction

Examples the prompt handles:
- "walking Biscuit this morning" → `{ type: "fact", key: "pet", value: "Has a dog named Biscuit" }`
- "my 3-year-old daughter loves Frozen" → `{ type: "fact", key: "child", value: "Has a daughter (age 3)" }`

### Embedding

Each extracted memory gets embedded via `text-embedding-3-small` and stored in `memory_embeddings`.

---

## Recall Pipeline

### End-to-end flow

```
Query: "Where does this user live?"
       │
       ▼
┌──────────────────┐
│ Query Rewriting  │  ← LLM rewrites for better retrieval
│                  │     "Where does this user live?" → "user current location city"
└──────┬───────────┘
       │
       ├──► Vector Search (pgvector cosine, top-20)
       │
       ├──► BM25 Search (tsvector rank, top-20)
       │
       ▼
┌──────────────────┐
│ Reciprocal Rank  │  ← Fuse both result sets
│ Fusion (RRF)     │     score = Σ 1/(k + rank_i), k=60
└──────┬───────────┘
       │
       ▼
┌──────────────────┐
│ LLM Reranking    │  ← Top fused candidates reranked by LLM
│                  │     for query relevance (top-10)
└──────┬───────────┘
       │
       ▼
┌──────────────────┐
│ Context Assembly │  ← Priority order under max_tokens budget:
│                  │     1. Active stable facts (high confidence)
│                  │     2. Query-relevant memories (reranked)
│                  │     3. Recent context from same session
└──────┬───────────┘
       │
       ▼
Formatted context string + citations
```

### Token Budget Logic

```
budget = max_tokens

1. Stable facts (type=fact, confidence>0.8, active=true)
   - Deduplicate by key (keep latest)
   - Estimate tokens: ~4 chars per token
   - Fill until 40% of budget used

2. Query-relevant memories (reranked results)
   - Fill until 80% of budget used

3. Recent session context
   - Last 2-3 turns from current session_id
   - Fill remaining budget

If budget < 256 tokens: skip recent context, only stable facts + top relevant
If budget < 128 tokens: only top 3 stable facts
```

### Priority Logic (defended in README)

**Stable facts first** because:
- They're high-confidence, compact, and almost always relevant
- A frozen LLM needs user context more than conversation replay
- Example: knowing "vegetarian, allergic to shellfish" is more useful than "user asked about React hooks last Tuesday"

**Query-relevant next** because:
- The recall query signals what the agent needs right now
- These are the "why are we even looking this up" results

**Recent context last** because:
- It's verbose and low signal density
- Useful as fallback but not worth displacing structured knowledge

---

## Fact Evolution Strategy

### Contradiction Detection

When new memories are extracted, for each one:

1. Look up existing active memories for same `user_id` with matching or similar `key`
2. Send both old and new to LLM with prompt:
   ```
   Given existing memory: "Works at Stripe as an engineer"
   And new extraction: "Just started at Notion"
   Are these about the same topic? Is the new one an update/contradiction/correction?
   ```
3. LLM returns: `{ same_topic: true, relationship: "update" | "contradiction" | "correction" | "new" }`

### Handling Each Case

| Relationship | Action |
|-------------|--------|
| `new` | Insert fresh memory |
| `update` | Insert new memory, set `supersedes = old_id`, mark old `active = false` |
| `contradiction` | Same as update, but set old confidence lower |
| `correction` | Same as update, new memory value includes correction context |
| `gradual_shift` (opinion arc) | Keep both active, new one has higher confidence, old one gets tag `evolving` |

### Opinion Arcs

"I love TypeScript" → "TypeScript generics are getting annoying" → "TypeScript is fine for big projects but I'd use Python for scripts"

Strategy:
- Don't supersede — these aren't contradictions, they're nuance additions
- Store each as separate active opinion with timestamp
- In recall, return the **latest** opinion with a note about evolution
- `/users/{user_id}/memories` shows the full arc ordered by `created_at`

Detection: LLM prompt explicitly asks "does this new opinion contradict the old one, or add nuance?"

---

## Project Structure

```
memory-service/
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── README.md
├── CHANGELOG.md
├── src/
│   ├── main.ts
│   ├── app.module.ts
│   │
│   ├── turns/
│   │   ├── turns.module.ts
│   │   ├── turns.controller.ts
│   │   ├── turns.service.ts
│   │   └── dto/
│   │       └── create-turn.dto.ts
│   │
│   ├── recall/
│   │   ├── recall.module.ts
│   │   ├── recall.controller.ts
│   │   ├── recall.service.ts
│   │   └── dto/
│   │       └── recall-query.dto.ts
│   │
│   ├── search/
│   │   ├── search.module.ts
│   │   ├── search.controller.ts
│   │   ├── search.service.ts
│   │   └── dto/
│   │
│   ├── memories/
│   │   ├── memories.module.ts
│   │   ├── memories.controller.ts
│   │   ├── memories.service.ts
│   │   ├── entities/
│   │   │   ├── memory.entity.ts
│   │   │   ├── turn.entity.ts
│   │   │   ├── memory-embedding.entity.ts
│   │   │   └── memory-search-text.entity.ts
│   │   └── dto/
│   │
│   ├── extraction/
│   │   ├── extraction.module.ts
│   │   ├── extraction.service.ts
│   │   └── prompts/
│   │       ├── extract.prompt.ts
│   │       └── contradiction.prompt.ts
│   │
│   ├── llm/
│   │   ├── llm.module.ts
│   │   └── llm.service.ts          # OpenAI wrapper
│   │
│   ├── embeddings/
│   │   ├── embeddings.module.ts
│   │   └── embeddings.service.ts   # Embedding generation
│   │
│   ├── common/
│   │   ├── guards/
│   │   │   └── auth.guard.ts       # Optional Bearer token
│   │   ├── filters/
│   │   │   └── all-exceptions.filter.ts
│   │   └── interceptors/
│   │       └── logging.interceptor.ts
│   │
│   └── database/
│       └── database.module.ts
│
├── tests/
│   ├── contract/
│   │   ├── turns.e2e-spec.ts
│   │   ├── recall.e2e-spec.ts
│   │   ├── search.e2e-spec.ts
│   │   ├── memories.e2e-spec.ts
│   │   └── cleanup.e2e-spec.ts
│   ├── persistence/
│   │   └── restart.e2e-spec.ts
│   ├── robustness/
│   │   └── malformed-input.e2e-spec.ts
│   └── recall-quality/
│       └── recall-fixture.e2e-spec.ts
│
└── fixtures/
    ├── conversations.ts            # 3-5 scripted conversations
    └── expected-facts.ts           # Expected facts per conversation
```

---

## Implementation Order (per CHANGELOG entry)

### v0 — Project scaffold
- NestJS init, Dockerfile, docker-compose.yml (Postgres + pgvector)
- Database module, entities, migrations
- Health endpoint
- **~2h**

### v1 — HTTP contract + basic storage
- All 7 endpoints implemented
- Turns stored as-is, memories as raw message chunks
- Recall returns last N turns (naive)
- **~3h**

### v2 — Extraction pipeline
- LLM-based extraction: raw turns → structured memories
- Store memories with type, key, value, confidence
- Embed memories on extraction
- `/users/{user_id}/memories` returns structured data
- **~3h**

### v3 — Hybrid recall with RRF
- pgvector cosine search (top-20)
- tsvector BM25 search (top-20)
- Reciprocal Rank Fusion
- Context assembly with token budget
- **~3h**

### v4 — Fact evolution
- Contradiction detection via LLM
- Supersedes chain
- Opinion arc handling
- **~3h**

### v5 — Recall improvements
- Query rewriting via LLM
- LLM reranking after RRF
- Priority tuning under budget
- **~2h**

### v6 — Tests, fixtures, polish
- Contract tests, persistence tests, concurrent sessions
- Recall quality fixture with self-eval
- README, CHANGELOG
- **~3h**

**Total: ~19 hours**

---

## Key Design Decisions to Defend

1. **PostgreSQL + pgvector over separate vector DB** — fewer moving parts, one container, FTS built-in, good enough for single-user scale
2. **Synchronous extraction** — simpler, no race conditions, 60s timeout is generous
3. **LLM for everything** (extraction, contradiction, reranking) — higher quality, lower engineering complexity than rule-based systems
4. **key normalization** — critical for contradiction detection ("employer" vs "job" vs "workplace" should match)
5. **Opinion arcs not superseded** — nuance matters, "fine for big projects" is not a contradiction of "annoying generics"
6. **NestJS over Python** — user preference, TypeScript type safety, modular DI architecture

---

## Environment Variables

```env
# Required
OPENAI_API_KEY=sk-...

# Optional
MEMORY_AUTH_TOKEN=           # If set, requires Bearer auth
PORT=8080                    # Default 8080
DATABASE_URL=postgresql://postgres:postgres@db:5432/memory
OPENAI_MODEL=gpt-4o-mini
EMBEDDING_MODEL=text-embedding-3-small
LOG_LEVEL=info
```
