# Hybrid Plan — Best of Both Solutions

> Цель: объединить сильную runtime-систему (PostgreSQL + LLM) с инженерной
> дисциплиной (deterministic baseline, zero-setup, hermetic tests,
> measurement-driven iteration).

---

## 0. Принцип гибридизации

| Из текущего решения (PG + LLM) | Из «different» (SQLite + Rules) |
|---|---|
| PostgreSQL 16 + pgvector + tsvector | Deterministic rule baseline (always-on) |
| LLM extraction (implicit facts) | `extra="forbid"` на всех request schemas |
| Query rewriting (multi-hop) | Zero-setup: работает без `.env` и API ключей |
| Cross-key contradictions (pgvector) | Hermetic test suite (no real API calls) |
| LLM reranking | Adversarial eval + fixture self-eval |
| Embeddings (text-embedding-3-small) | Canonical keys + evolution.py (mutable/append-only) |
| RRF fusion | Measurement discipline: каждое решение измерено |
| Adaptive noise gating | Token budget priority: stable → query-relevant → recent |
| | No DB transaction across LLM/network call |
| | 127+ hermetic pytest, guardrails noise/scope = 1.0 |

---

## 1. Архитектура (итоговая)

```
                          ┌──────────────────── FastAPI (api/) ────────────────────┐
  client ──HTTP──▶  /health /turns /recall /search /users/{id}/memories           │
                     /sessions/{id} /users/{id}                                   │
                     Pydantic contract: extra="forbid" → 422 on unknown fields    │
                     Optional Bearer auth (MEMORY_AUTH_TOKEN)                      │
                          └────┬──────────────┬────────────────┬──────────────────┘
                               │              │                │
                    POST /turns         POST /recall    GET /users…/memories
                               │              │                │
                          ┌────▼────┐   ┌─────▼──────┐   ┌────▼──────┐
                          │extraction│   │   recall   │   │  storage  │
                          │ pipeline │   │  pipeline  │   │   layer   │
                          └────┬────┘   └─────┬──────┘   └────┬──────┘
                               │              │                │
              ┌────────────────┼───────┐      │                │
              │                │       │      │                │
     ┌────────▼──────┐ ┌──────▼─────┐ │ ┌────▼─────┐   ┌─────▼──────┐
     │ rules.py      │ │ llm_       │ │ │ query_   │   │ SQLAlchemy │
     │ (always-on,   │ │ extractor  │ │ │ rewrite  │   │ 2.0 Async  │
     │  no API key)  │ │ (optional, │ │ │          │   │ Repos      │
     │               │ │  default-  │ │ │ vector   │   │            │
     │ canonicalize  │ │  OFF)      │ │ │ search   │   │ TurnRepo   │
     │ evolution.py  │ │            │ │ │ (pgvector│   │ MemoryRepo │
     │ (mutable/     │ │ provider   │ │ │  HNSW)   │   └─────┬──────┘
     │  append-only) │ │ seam       │ │ │          │         │
     └────────┬──────┘ └─────┬──────┘ │ │ bm25     │         │
              │              │        │ │ (tsvector│         │
              └──────┬───────┘        │ │  + GIN)  │         │
                     │                │ │          │         │
                     │                │ │ RRF +    │         │
              merge_candidates        │ │ rerank   │         │
              (rules authoritative)   │ │          │         │
                     │                │ │ noise    │         │
                     ▼                │ │ gate     │         │
              ┌──────────────┐        │ └──────────┘         │
              │ memories +   │        │                      │
              │ embeddings   │        │                      │
              │ (batch)      │        │                      │
              └──────┬───────┘        │                      │
                     │                │                      │
                     ▼                ▼                      ▼
              ┌──────────────────────────────────────────────────┐
              │              PostgreSQL 16                        │
              │  + pgvector (HNSW)    — vector search            │
              │  + tsvector (GIN)     — BM25 keyword search      │
              │  Named volume: pgdata — persists across restarts  │
              └──────────────────────────────────────────────────┘
```

### POST /turns flow (synchronous, read-your-write)

1. Persist raw turn + messages → **commit** (short transaction)
2. **Deterministic rule extraction** — pure Python, no DB, no API key
   - Regex patterns over user messages only
   - Canonical keys, scope assignment, intra-payload last-wins
3. **Optional LLM extraction** — if `LLM_ENABLED=true` AND key present
   - Runs OUTSIDE any DB transaction
   - Failure → rules-only (service stays green)
   - Provider seam: OpenAI SDK (locked dependency), Gemini-via-httpx ready
4. **merge_candidates(rules, llm_candidates)** — rules are authoritative on conflicts
5. **Contradiction check** — same-key (vs most recent) + cross-key (pgvector sim > 0.80)
6. **Supersession** — mutable keys: deactivate old, insert new; append-only: coexist
7. **Batch embed** — all new memories in one API call (if key present)
8. Write memories + FTS index → **commit** (second short transaction)
9. Return `201 {"id": ...}` — data immediately queryable

**No DB transaction is held across the LLM/network call.**

---

## 2. Extraction Pipeline — Hybrid (Rules + LLM)

### 2.1 Deterministic Rule Baseline (always-on)

Всегда работает, даже без API ключа. Precision-first.

```python
# src/services/rule_extractor.py — enhanced with canonical keys

CATEGORY_PATTERNS = {
    "employment": [
        r"(?i)\b(?:i (?:work|am working|am employed) (?:at|for|with))\s+(.+?)(?:\.|,|$)",
        r"(?i)\b(?:my (?:job|role|position|title) (?:is|was))\s+(.+?)(?:\.|,|$)",
        r"(?i)\b(?:i (?:just (?:joined|started|hired)|was hired|got a job))\s+(?:at\s+)?(.+?)(?:\.|,|$)",
    ],
    "location": [
        r"(?i)\b(?:i (?:live|am living|am based|reside|moved))\s+(?:in|to)\s+(.+?)(?:\.|,|$)",
        r"(?i)\b(?:my (?:city|hometown|location))\s+(?:is)\s+(.+?)(?:\.|,|$)",
    ],
    "pet": [
        r"(?i)\b(?:i (?:have|got|own)\s+a\s+(.+?)(?:named|called)\s+(\w+))",
        r"(?i)\b(?:my\s+(\w+)\s+(?:named|called)\s+(\w+))",
        r"(?i)\b(?:walking|feeding|playing with)\s+(\w+)\s+(?:this|that|the|every)\s",
    ],
    "allergy": [
        r"(?i)\b(?:i(?:'m| am) (?:allergic|intolerant)\s+to)\s+(.+?)(?:\.|,|$)",
        r"(?i)\b(?:i (?:have|got) (?:a\s+)?(?:allerg\w+|intolerance)\s+(?:to|with)?)\s+(.+?)(?:\.|,|$)",
    ],
    "diet": [
        r"(?i)\b(?:i(?:'m| am) (?:vegetarian|vegan|pescatarian|keto|paleo|gluten-free))\b",
        r"(?i)\b(?:i (?:don't|do not) eat)\s+(.+?)(?:\.|,|$)",
        r"(?i)\b(?:my (?:diet|food preference|dietary restriction))\s+(?:is)\s+(.+?)(?:\.|,|$)",
    ],
    "communication": [
        r"(?i)\b(?:i (?:prefer|like|want|need) (?:\w+\s+)?(?:concise|detailed|brief|short|long|direct|chatty|formal|casual))",
        r"(?i)\b(?:please (?:be|keep|give|respond)\s+(.+?)(?:\.|,|$))",
        r"(?i)\b(?:i (?:prefer|like|want) (?:my\s+)?(?:answers?|responses?|replies?|format))\s+(?:to be\s+)?(.+?)(?:\.|,|$)",
    ],
    "opinion": [
        r"(?i)\b(?:i (?:love|hate|like|dislike|enjoy|prefer|think|believe|feel))\s+(.+?)(?:\.|,|$)",
    ],
    "correction": [
        r"(?i)\b(?:actually|sorry|i meant|i meant to say|correction|my mistake|not\s+\w+\s*[-—]\s*)(.+?)(?:\.|,|$)",
    ],
}
```

**Subject gates:**
- Only user messages (`role="user"`) are extracted — assistant/tool text is never extracted
- First-person anchored ("I work at…") for personal facts
- Third-party suppressed ("my friend works at…" → ignored)
- Opinions: relaxed first-person, but attribution-suppressed

**Canonical keys** (from different approach):

```python
# src/extraction/canonicalize.py

CANONICAL_MAP = {
    # Employment
    "employer": "employment.company",
    "company": "employment.company",
    "workplace": "employment.company",
    "job": "employment.role",
    "role": "employment.role",
    "position": "employment.role",
    "title": "employment.role",

    # Location
    "city": "location.current_city",
    "location": "location.current_city",
    "lives_in": "location.current_city",
    "hometown": "location.hometown",       # deliberately separate from current
    "previous_city": "location.previous_city",
    "moved_from": "location.previous_city",

    # Pets
    "pet": "pet.type",
    "dog": "pet.type",
    "cat": "pet.type",
    "pet_name": "pet.name",

    # Diet & Allergies
    "diet": "diet.preference",
    "dietary_restriction": "diet.restriction",
    "allergy": "allergy.substance",

    # Communication
    "communication_style": "communication.style",
    "preferred_response_format": "communication.format",

    # Generic
    "preference": "preference.{normalized}",
    "opinion": "opinion.{topic}",
    "event": "event.{description}",
}
```

### 2.2 LLM Extraction (optional, default-OFF)

```python
# src/extraction/llm_extractor.py

class LLMExtractor:
    """
    Optional LLM extraction arm. Failure-tolerant: any error → return [].

    Candidates are strict-schema validated, grounded (value is a normalized
    span of a user message OR explicitly allowlisted implicit inference),
    third-party-suppressed, confidence-capped, and merged with deterministic
    rules winning conflicts.
    """

    def __init__(self, config: Config):
        self.enabled = config.LLM_ENABLED  # default: false
        self.provider = config.LLM_PROVIDER  # "openai"
        self.model = config.OPENAI_MODEL  # "gpt-4o-mini"
        self.timeout = config.LLM_TIMEOUT_SECONDS
        self.confidence_cap = config.LLM_CONFIDENCE_CAP  # 0.85
        self.max_candidates = config.LLM_MAX_CANDIDATES  # 10

    async def extract(self, messages: list[Message]) -> list[LLMCandidate]:
        if not self.enabled:
            return []
        try:
            # LLM call — NO DB transaction open
            raw = await self._call_llm(messages)
            candidates = self._validate_and_ground(raw, messages)
            return self._suppress_third_party(candidates)
        except Exception:
            log.warning("LLM extraction failed, falling back to rules-only")
            return []
```

### 2.3 Merge Strategy (rules authoritative)

```python
def merge_candidates(
    rules: list[MemoryCandidate],
    llm: list[LLMCandidate],
) -> list[MemoryCandidate]:
    """
    Rules are authoritative: if both extract the same canonical key,
    the rule-based value wins. LLM candidates only fill gaps that
    rules missed (implicit facts, paraphrased facts).
    """
    rule_keys = {r.canonical_key for r in rules}
    merged = list(rules)

    for candidate in llm:
        if candidate.canonical_key not in rule_keys:
            merged.append(MemoryCandidate(
                canonical_key=candidate.canonical_key,
                value=candidate.value,
                confidence=min(candidate.confidence, CONFIDENCE_CAP),
                type=candidate.type,
                evidence=candidate.evidence,
                scope=candidate.scope,
            ))

    return merged
```

---

## 3. Fact Evolution — Canonical Keys + Cross-Key

### 3.1 Key Classification (from different approach)

```python
# src/extraction/evolution.py

MUTABLE_KEYS = {
    "employment.company", "employment.role",
    "location.current_city",
    "communication.style", "communication.format",
    "diet.preference",
    # All preference.* and opinion.* are mutable
}

APPEND_ONLY_KEYS = {
    "event.*",           # events don't overwrite
    "allergy.substance", # allergies accumulate
    "pet.type", "pet.name",  # pets accumulate
    "location.previous_city",  # history
    "location.hometown",       # origin
}
```

### 3.2 Supersession Rules

| Scenario | Key Type | Behavior |
|---|---|---|
| New value differs from current | Mutable | Old → `active=False`, new → `active=True, supersedes=old_id` |
| Exact re-affirmation | Mutable | Dedupe (no new row) |
| New value for append-only key | Append-only | Coexists (both active) |
| Opinion change | Mutable | Latest-stance-per-topic, prior stances preserved in chain |
| Cross-key conflict (pgvector sim > 0.80) | Any | LLM classifies: `update` → deactivate old, `merge` → absorb, `independent` → keep both |

**Nothing is ever deleted on update.** History preserved via `supersedes` chain.

---

## 4. Recall Pipeline — 7-Stage Hybrid

```
POST /recall
     │
     ▼
 1. Query rewriting (LLM) ──────── short queries (≤4 words) skip
     │                               multi-hop: decompose into 2-3 sub-queries
     ▼
 2. Sub-query embedding (batch) ─── one API call for all sub-queries
     │
     ▼
 3. Vector search (pgvector HNSW) ─ top-20 per sub-query, cosine similarity
     │
     ▼
 4. BM25 search (tsvector GIN) ──── top-20 per sub-query, websearch_to_tsquery
     │                               + canonical key match (from rules)
     │                               + LIKE fallback for FTS-hostile input
     ▼
 5. RRF fusion (k=60) ───────────── merge + dedupe by memory ID
     │                               + active/confidence/recency/same-session boosts
     │                               + current-vs-history intent gating
     ▼
 6. LLM reranking ───────────────── top-15 fused candidates
     │                               multi-hop grouping (memories that jointly answer)
     │                               noise gate: adaptive floor (0.35 min, 0.50 breakpoint)
     ▼
 7. Context assembly (budget) ───── markdown prose + deduped citations
                                  priority: (1) stable facts → (2) query-relevant → (3) recent
                                  noise → empty: {"context":"","citations":[]}
```

### 4.1 Token Budget Priority (defended)

| Priority | Allocation | Content | Rationale |
|---|---|---|---|
| 1 | 35% (or 0%) | Stable facts (confidence ≥ 0.8, cosine sim ≥ 0.35 to query) | Compact, high-value. **Skipped if < 30% pass relevance threshold** — budget redistributed. |
| 2 | 50% (or 60%) | Query-relevant memories (reranked) | Query is the strongest signal of what the agent needs. Gets 60% when stable facts skipped. |
| 3 | 15% (or 40%) | Recent session turns | Verbose, low density. Gets 40% when stable facts skipped. |

### 4.2 Noise Resistance (from different approach, enhanced)

```
if best_reranked_similarity < 0.50:
    threshold = 0.35                    # strict floor for all results
else:
    threshold = max(0.35, best_sim * 0.5)

if count(stable_facts passing threshold) < 0.30 * count(all_stable_facts):
    skip stable facts section entirely
    redistribute: 60% query-relevant, 40% recent

unrelated query → {"context": "", "citations": []}  # 200 OK, no hallucination
```

### 4.3 Multi-hop Strategy (dual approach)

1. **Query rewriting** (from current) — LLM decomposes complex queries into sub-queries
2. **Canonical key matching** (from different) — structured match against hint vocabulary
3. **1-hop entity expansion** (from different) — "city of the person whose dog is Biscuit"
4. **LLM reranker grouping** (from current) — which memories *jointly* answer the query

---

## 5. Backing Store — PostgreSQL 16 + pgvector

**Why PG, not SQLite (from different approach):**
- pgvector HNSW gives true semantic search — measured necessary for multi-hop
- `tsvector` + GIN gives production-grade BM25
- Cross-key contradiction detection via pgvector similarity (cosine > 0.80)
- Named volume `pgdata` persists across restarts
- Docker Compose: single `docker compose up` boots both app + Postgres

**Why NOT SQLite (rejecting from different):**
- Different approach measured `semantic_gap = 0.0` on *their* adversarial proxy
- Our internal evaluation shows multi-hop queries *do* need semantic search
- pgvector + tsvector in one container is still zero-ops
- Cross-key contradictions require vector similarity — SQLite has no native vector search

**Tradeoff:** requires Postgres container. Justified: the challenge allows `docker-compose.yml` to define deps, and the eval runs `docker compose up` (not just `docker run`).

---

## 6. Zero-Setup + Graceful Degradation

```python
# src/config.py

class Config(BaseSettings):
    # --- Always works without these ---
    OPENAI_API_KEY: str | None = None
    LLM_ENABLED: bool = False              # default OFF
    LLM_PROVIDER: str = "openai"

    # --- Extraction behavior ---
    # No key / LLM disabled → rules-only extraction (structured memories still produced)
    # Key present + LLM_ENABLED=true → rules + LLM, rules authoritative on conflicts
```

**Degradation ladder:**

| State | Extraction | Recall | Embeddings |
|---|---|---|---|
| No `.env`, no key | Rules only (structured, typed) | BM25 + canonical key + recency | None (cosine unavailable) |
| Key present, LLM off | Rules only | BM25 + canonical key + recency | None |
| Key present, LLM on | Rules + LLM, rules win conflicts | Full hybrid (vector + BM25 + rerank) | Batch per extraction |
| LLM timeout/error | Fallback to rules-only | Full hybrid (existing embeddings still searchable) | N/A |

**Service always returns `201` on `/turns` and `200` on `/recall`. Never `5xx` from extraction failure.**

---

## 7. Schema — Pydantic `extra="forbid"`

```python
# src/schemas/turn.py

class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(..., pattern=r"^(user|assistant|tool)$")
    content: str
    name: str | None = None       # tool name attribution

class TurnCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    user_id: str | None = None
    messages: list[MessageCreate]
    timestamp: datetime
    metadata: dict = Field(default_factory=dict)

# src/schemas/recall.py
class RecallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    session_id: str
    user_id: str | None = None
    max_tokens: int = 1024

# src/schemas/search.py
class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    session_id: str | None = None
    user_id: str | None = None
    limit: int = 10

class SearchResult(BaseModel):
    content: str
    score: float
    session_id: str
    timestamp: str
    metadata: dict = {}
    key: str | None = None
    type: str | None = None
```

---

## 8. Test Strategy — Hermetic + Adversarial

### 8.1 Hermetic Suite (no real API, even with local `.env`)

```python
# tests/conftest.py

import os

# Force LLM off and drop keys BEFORE app imports
os.environ["LLM_ENABLED"] = "false"
os.environ.pop("OPENAI_API_KEY", None)

import pytest
from httpx import AsyncClient, ASGITransport
from src.main import create_app

@pytest.fixture
async def client():
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as c:
        yield c
```

### 8.2 Test Coverage (target: 120+ tests)

| Category | Count | What |
|---|---|---|
| Contract roundtrip | 16 | All 7 endpoints, status codes, shapes, `extra="forbid"` → 422 |
| Persistence | 3 | Write → `down` → `up` → recall, named volume verification |
| Concurrent sessions | 5 | No cross-user bleed, user-scoped sharing works |
| Malformed input | 10 | Bad JSON, missing fields, unicode, FTS-hostile, SQL injection, oversized |
| Extraction (rules) | 15 | Each category pattern, subject gates, third-party suppression |
| Extraction (LLM mock) | 10 | Mocked LLM responses, grounding, confidence cap, merge |
| Extraction (merge) | 8 | Rules win conflicts, LLM fills gaps, dedup |
| Evolution | 12 | Mutable supersession, append-only, exact re-affirmation, delete-safety |
| Cross-key contradictions | 5 | pgvector sim > 0.80, LLM classify, deactivate old |
| Recall quality | 12 | Fixture probes: single-hop, multi-hop, keyword, cross-session |
| Noise resistance | 8 | Unrelated query → empty, adaptive floor, density gate |
| Scope isolation | 6 | Session-scoped, user-scoped, no bleed |
| Auth | 4 | Optional bearer, no auth when unset |
| LLM isolation | 2 | Prove no real API call even with `.env` present |

### 8.3 Self-Eval Fixture

```
fixtures/
├── conversations.json   # 5 scripted conversations (2 users, 5 sessions)
└── probes.json          # 67+ probes with must_match + must_not + gated
```

Deterministic grading (no LLM-as-judge):
- `must_match`: list of strings that MUST appear in context
- `must_not`: list of strings that MUST NOT appear
- `gated`: conditional probes that only run if earlier probes pass

### 8.4 Adversarial Eval (dev/test only)

```python
# tests/adversarial_eval.py
# ~100 natural-language gap diagnostic cases
# Attributes every miss to: extraction-gap / recall-vocab / recall-lexical / semantic / acceptable-limit
```

---

## 9. Implementation Priority

| Phase | What | Est. | Impact |
|---|---|---|---|
| **P0** | Add `rule_extractor.py` with canonical keys + evolution | 4h | Service works without API key |
| **P1** | Add `extra="forbid"` to all schemas | 0.5h | Contract compliance, prevents silent field dropping |
| **P2** | Make LLM extraction optional behind `LLM_ENABLED` flag | 2h | Zero-setup, graceful degradation |
| **P3** | Ensure no DB txn across LLM call | 1h | Correctness under failure |
| **P4** | Hermetic test suite (`conftest.py` + isolation test) | 2h | Reproducible, CI-friendly |
| **P5** | Adversarial eval + fixture expansion to 67 probes | 3h | Measurable quality regression |
| **P6** | Noise resistance hardening (density gate, current-vs-history) | 2h | Unrelated queries → empty |
| **P7** | Cross-key contradiction via pgvector similarity | 3h | Detects conflicts across different keys |
| **P8** | Query rewriting + multi-hop reranker grouping | 3h | Multi-hop recall |
| **P9** | README rewrite (16 sections, defended tradeoffs) | 2h | Human review |
| **P10** | CHANGELOG with per-phase measurements | 1h | Engineering process evidence |

**Total: ~23h** — fits within the 2-day timebox.

---

## 10. CHANGELOG Template (measurement-driven)

Each entry must have **What changed · Why · Result (with metrics) · Next**:

```markdown
## vX — Title

**What changed:** Concrete description of what was added/modified.

**Why:** What was observed (with metric) that motivated the change.

**Result:**
- pytest: N passed (hermetic)
- fixture_eval: N/N, metrics X.X
- adversarial_eval: extraction X.XX, recall X.XX, semantic_gap X.XX
- internal QA: N PASS / N PARTIAL / N FAIL

**Next:** What's the measured gap to close next.
```

---

## 11. README Structure (16 sections)

1. Quickstart (Docker Compose)
2. Architecture (diagram + prose)
3. HTTP contract (with examples)
4. Backing store choice — what and why
5. Extraction pipeline — structured, not chunks
6. Fact evolution / supersession
7. Recall & search strategy
8. Scope correctness
9. Optional LLM extraction (default-OFF)
10. Why embeddings / semantic recall
11. Tradeoffs
12. Failure modes
13. Development & tests
14. Evaluation evidence (internal, committed)
15. Configuration
16. Submission checklist

---

## 12. Key Differences from Current Implementation

| Area | Current | Hybrid |
|---|---|---|
| Extraction | LLM-only | Rules (always-on) + LLM (optional) |
| No API key | Extraction fails silently, no memories | Rules produce structured memories |
| Schemas | No `extra="forbid"` | All request schemas reject unknown fields |
| DB txn across LLM | Current code may hold txn | Explicit: commit before LLM, second commit after |
| Tests | 33, some need Docker | 120+ hermetic pytest + adversarial eval |
| Recall w/o embeddings | Degraded | BM25 + canonical keys still work |
| Canonical keys | Free-form LLM output | Controlled vocabulary with merge rules |
| Evolution | LLM classifies relationship | Key classification (mutable/append-only) + LLM for cross-key |
| Noise resistance | Adaptive thresholds | Adaptive thresholds + density gate + empty-on-cold |
| Zero-setup | Requires `.env` + key | `docker compose up` — fully functional |
| CHANGELOG | 8 entries, good narrative | Per-phase measurements + gap analysis |

---

## 13. What NOT to Take from Different Approach

| From Different | Why Not |
|---|---|
| SQLite + FTS5 | pgvector needed for semantic search + cross-key contradictions |
| `semantic_gap = 0.0` | Measured on their fixtures; our multi-hop scenarios show real semantic need |
| 1-hop only | Query rewriting + reranker grouping is stronger for multi-hop |
| No embeddings | Embeddings are necessary for cross-key contradiction detection |
| `hometown` deliberately excluded | We should extract it but tag as `location.hometown` (≠ current_city) |

---

## 14. Submission Checklist

- [ ] `docker compose up` boots with no manual steps; port 8080
- [ ] Persists across `docker compose down && up` (named volume)
- [ ] Exact HTTP contract (shapes/status codes; `extra="forbid"` → 422)
- [ ] Structured typed memories with provenance + supersession (not chunks)
- [ ] Hybrid recall (not vanilla cosine); noise → empty; budgeted prose
- [ ] Synchronous read-your-write; no DB txn across the LLM call
- [ ] Zero-setup: works without `.env` (rules-only extraction)
- [ ] Optional LLM extraction, default-off, key-free fallback; hermetic tests
- [ ] Deterministic rule baseline (always-on, no API key)
- [ ] 120+ hermetic pytest · self-eval 67/67 · adversarial eval
- [ ] `ruff` clean
- [ ] `README.md` (16 sections) + measurement-driven `CHANGELOG.md`
- [ ] No secrets / `.env` / `*.db` committed; no AI/co-author attribution
