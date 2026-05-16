# memory-service

A Dockerized memory service for an AI agent (Higgsfield AI engineering
challenge). It ingests conversation turns, extracts **structured, typed
memories** (not raw message chunks), evolves facts over time
(contradictions supersede, history preserved), and answers hybrid
`/recall` and `/search` queries that decide what context the agent sees
next. Single FastAPI service + SQLite (WAL + FTS5) on a Docker named
volume. No external services.

> **Note on metrics:** every number below comes from the project's own
> **internal proxy evals** (`tests/fixture_eval.py`,
> `tests/adversarial_eval.py`, the per-phase 200-case QA reports). They
> are deterministic regression instruments, **not** the Higgsfield
> private eval. Wording is deliberate (e.g. "internal adversarial recall
> proxy 0.7097 → 0.9355"), not a claim about the private eval.

---

## 1. Quickstart (Docker Compose)

No setup steps. From a clean checkout:

```bash
docker compose up --build -d --wait
curl -sf localhost:8080/health        # {"status":"ok"}
docker compose down                   # stop (named volume persists)
docker compose up -d                  # data survives the restart
docker compose down -v                # full teardown (drops the volume)
```

The service binds port **8080**; SQLite lives at `/data/memory.db` on the
named volume `memory-data`. It runs **fully with no `.env`** and **no API
keys** (deterministic rule-based extraction is the always-on baseline).

## 2. Architecture

```
                 ┌──────────────────────── FastAPI (api/) ─────────────────────────┐
 client ──HTTP──▶ │ /health /turns /recall /search /users/{id}/memories             │
                 │ /sessions/{id}  · thin routes · Pydantic contract (extra=forbid) │
                 └───────┬───────────────────┬───────────────────────┬─────────────┘
                         │ POST /turns        │ POST /recall|/search  │ GET /users…
                         ▼                    ▼                       ▼
              extraction/                recall/                 storage/
              ├ rules.py (regex,         ├ query.py (intent +     repositories.py
              │  always-on baseline)     │  hint vocab)           over SQLite
              ├ canonicalize.py          ├ retrieval.py (gather)  (sqlite.py: WAL,
              ├ evolution.py             ├ scoring.py (fuse+      foreign_keys=ON,
              │  (supersession)          │  boosts+noise gate)    busy_timeout,
              └ llm_extractor.py         └ context.py (budgeted   1 conn + write
                 (OPTIONAL, default-off)    prose + citations)    lock, FTS5)
                                                                       │
                                                          ┌────────────▼────────────┐
                                                          │ SQLite @ /data/memory.db │
                                                          │ named volume memory-data │
                                                          └──────────────────────────┘
```

**`POST /turns` is synchronous and read-your-write — no eventual
consistency.** The flow keeps **no DB transaction open across the
(optional) LLM/network call**:

1. Persist the raw turn + messages in a short transaction, **commit**.
2. Deterministic rule extraction (pure, no DB, no open txn).
3. *Optional* LLM extraction — runs here, **outside any DB transaction**;
   any failure → rules-only.
4. Validate/ground/merge → write memories + FTS index in a **second**
   short transaction.
5. Return `201 {"id": ...}` only once the data is queryable.

Thin routes; logic lives in `storage` / `extraction` / `recall`. Stdlib
logging. Settings via pydantic-settings; no secrets in code.

## 3. HTTP contract (with examples)

| Method & path | Status | Body / response |
|---|---|---|
| `GET /health` | 200 | `{"status":"ok"}` (unauthenticated) |
| `POST /turns` | 201 | `{session_id, user_id?, messages:[{role,content,name?}], timestamp, metadata}` → `{"id": str}` |
| `POST /recall` | 200 | `{query, session_id, user_id?, max_tokens=1024}` → `{"context": str, "citations":[{turn_id,score,snippet}]}` |
| `POST /search` | 200 | `{query, session_id?, user_id?, limit=10}` → `{"results":[{content,score,session_id,timestamp,metadata}]}` |
| `GET /users/{user_id}/memories` | 200 | `{"memories":[{id,type,key,value,confidence,source_session,source_turn,created_at,updated_at,supersedes,active,canonical_key,evidence}]}` |
| `DELETE /sessions/{session_id}` | 204 | deletes that session's turns/messages/FTS + memories sourced from it |
| `DELETE /users/{user_id}` | 204 | deletes that user's scoped memories + turns/sessions/messages/FTS |

Request bodies are `ConfigDict(extra="forbid")` → unknown fields → `422`.
Malformed / oversized / unicode / FTS-hostile input → `4xx`/empty `200`,
never a crash or `500`.

```bash
curl -s -X POST localhost:8080/turns -H 'Content-Type: application/json' -d \
 '{"session_id":"s1","user_id":"u1","messages":[{"role":"user","content":"I work at Stripe"}],"timestamp":"2025-01-01T00:00:00Z","metadata":{}}'
# {"id":"49c3f2a2-..."}
curl -s -X POST localhost:8080/recall -H 'Content-Type: application/json' -d \
 '{"query":"where does the user work","session_id":"s2","user_id":"u1"}'
# {"context":"## Known facts about this user\n- Works at Stripe","citations":[{"turn_id":"49c3f2a2-...","score":11.27,"snippet":"I work at Stripe"}]}
```

**Auth:** optional bearer. If `MEMORY_AUTH_TOKEN` is set, all endpoints
except `/health` require `Authorization: Bearer <token>`; if unset, auth
is not enforced.

## 4. Backing store choice — SQLite (WAL) + FTS5, named volume

**Why:** the contract is a single Docker-deployable service that must
boot with `docker compose up` (no setup) and persist across restarts.
SQLite gives that with **zero operational surface**: one file at
`/data/memory.db` on a named volume, transactional, no separate service
or network hop, FTS5/BM25 built in. WAL mode + `foreign_keys=ON` +
`busy_timeout` + a single connection guarded by a write lock + short
transactions. The schema (turns, messages, memories, memory_entities,
FTS5 virtual tables) is designed once up front — the DB is
volume-persisted so there is deliberately no migration story (per the
challenge's no-scale constraint).

**Tradeoff:** single-writer. For a hosted private eval (not a
high-concurrency production system) this is the right simplicity/
correctness trade; Postgres+pgvector / Qdrant would add a service,
credentials, and ops weight for no contract benefit here.

## 5. Extraction pipeline — structured, not chunks

`/users/{id}/memories` returns **typed memories** — `type ∈
fact|preference|opinion|event`, a stable `canonical_key`, `value`,
`confidence`, `evidence`, provenance (`source_session`, `source_turn`),
`scope`, `active`, `supersedes` — never raw message text.

- **Deterministic rule baseline (always on, no key):** conservative,
  I-anchored regex over **user** messages only (assistant/tool text is
  never extracted). Covers employment (company/role), location
  (current/previous city), pets, allergies, diet
  (preference/restriction), communication style, opinions/preferences,
  events, and corrections (forward / reversed / cue-gated, resolved
  against the existing scoped antecedent — never fabricated).
  `canonicalize.py` maps raw keys to stable canonical keys so
  contradictions are detectable across phrasings/sessions.
- **What it misses, and why:** precision-first regex deliberately skips
  most implicit ("walking Biscuit" → has a dog) and heavily paraphrased
  facts. Measured internal adversarial extraction proxy with rules only
  ≈ **0.14**; this is an intentional trade (no false memories) and is
  exactly what the optional LLM arm targets.
- **Optional LLM upgrade (§9).**

## 6. Fact evolution / supersession

`evolution.py` classifies canonical keys as **mutable** (one current
truth — employment.company/role, location.current_city,
communication.style, diet.preference, `preference.*`, `opinion.*`,
`relationship.*`) vs **append-only** (events, allergens, pets,
`location.previous_city`). A new, different value on a mutable key in the
same scope marks the prior row `active=0`, inserts the new one
`active=1` with `supersedes` → the old id; exact re-affirmations dedupe;
append-only values coexist. **Nothing is ever deleted on update** —
`/recall` returns the current fact, `/users/{id}/memories` shows the
full chain. The self-referential `supersedes` FK is delete-safe (inbound
pointers nulled before a referenced row is removed). Opinion changes are
modelled as latest-stance-per-topic with the prior stances preserved in
the chain.

## 7. Recall & search strategy

Deliberately **not** vanilla cosine-top-k. `/recall` runs a scope-
filtered hybrid:

- **structured** canonical-key + entity match (query → hint vocabulary
  in `query.py`),
- **FTS5/BM25** over memories + messages (safe MATCH; `LIKE` fallback;
  malformed query → skip, never `500`),
- **1-hop multi-hop** entity expansion (e.g. "city of the person whose
  dog is Biscuit"),
- fused (RRF-style) then boosted by **active / confidence / recency /
  same-session**; current-vs-history intent gating so a superseded value
  never reads as current.

**Token budget (priority when tight):** `context.py` assembles
approximately within `max_tokens` (~4 chars/token, never > ~2×) in
priority order: **(1) stable active user facts → (2) query-relevant
memories → (3) recent context**, as readable markdown prose +
deduped citations (each → a real `turn_id`). **Noise → empty:**
never-discussed queries return `{"context":"","citations":[]}` (200),
no hallucination. `/search` runs the same retrieval but returns ranked
structured results (no prose); with neither `user_id` nor `session_id`
it returns `[]` (no global search).

## 8. Scope correctness

Every memory carries `scope_type`/`scope_id`. `user_id` present →
user-scoped (shared across that user's sessions). `user_id` null →
session-scoped (must not bleed across sessions). `/recall` and `/search`
filter by the matching scope; verified by the isolation tests and the
adversarial scope guardrail (internal proxy: 1.0).

## 9. Optional LLM extraction (default-OFF)

A failure-tolerant LLM arm layered on top of the rule baseline behind a
provider seam (`extraction/llm_extractor.py`). **Off by default** — even
with a key it is only constructed when `LLM_ENABLED=true`. No key /
disabled / timeout / invalid JSON / provider error → **rules-only**,
service stays `201`/green. Provider = the already-locked `openai` SDK
(**no new dependency**; Gemini-via-httpx is seam-ready; Vertex AI
intentionally unsupported — no cloud credentials at startup).

Candidates are strict-schema validated, **grounded** (value is a
normalized span of a user message, or an explicitly allowlisted implicit
inference with `metadata.inferred=true`), third-party-suppressed,
confidence-capped, and merged with **deterministic rules winning
conflicts** — through the *same* canonicalize/evolution/scope path. The
LLM call holds **no DB transaction**. With the LLM disabled the merge is
identity (`merge_candidates(rules, []) is rules`) so default behaviour is
byte-identical and the self-eval is unchanged. Internal mocked-LLM
adversarial extraction proxy ≈ **0.76** vs **0.14** rules-only.

The **deterministic test suite is hermetic**: `tests/conftest.py` and
`tests/fixture_eval.py` pin `LLM_ENABLED=false` and drop
`OPENAI_API_KEY` before every app build, so `pytest` never calls a real
provider even with a developer `.env` (`tests/test_llm_isolation.py`
proves it). A **real-provider smoke is optional and manual** (a
throwaway script, never `pytest`, key never printed) and is **not** part
of the mandatory QA.

## 10. Why embeddings / semantic recall are deferred

Measured, not assumed: the internal adversarial NL-gap diagnostic
reported `semantic_gap = 0.0` (every recall miss had a lexical or
vocabulary bridge, none required semantics). **Phase 7.1** closed those
gaps deterministically with a tiny, guarded synonym/stemming widening of
the recall hint vocabulary — **internal adversarial recall proxy 0.7097
→ 0.9355** — **without embeddings, sqlite-vec, a vector DB, or a
reranker, and with zero new dependencies**. A dense arm would add an
offline model dependency for, by measurement, no gain; it is revisited
only if a future measured true-semantic share reaches ≥ 0.10–0.15. Two
residual recall misses (`hometown` ≠ current city; "what do they do for
a living" is ambiguous with location) are kept as **documented
ambiguity limits** rather than risk a false fact.

## 11. Tradeoffs

Optimized for: exact contract compliance, zero-setup reproducibility,
deterministic & defensible behaviour, real structured extraction +
evolution, and a strong evidence trail. Given up: semantic recall (until
measured-needed), single-writer SQLite (fine at eval scale), and
rule-extraction recall ceiling on novel phrasings (mitigated by the
optional LLM arm and the synonym widening).

## 12. Failure modes

- **No data / never-discussed query:** `/recall` →
  `{"context":"","citations":[]}` (200) — no hallucination.
- **Slow disk:** WAL + short transactions + `busy_timeout`; no
  transaction is held across the LLM/network call.
- **Missing API keys / LLM disabled / LLM error/timeout:** deterministic
  rule extraction only — fully functional, no `5xx`.
- **Malformed / oversized / unicode / FTS-hostile / SQL-ish input:**
  `4xx` or empty `200`, never a crash or `500`.
- **Container restart:** invisible to clients — data persists on the
  named volume (proven: a recall in a *new* session after
  `down`→`up` returns the same fact and the same `turn_id`).

## 13. Development & tests

```bash
uv sync
uv run uvicorn memory_service.main:app --reload   # local dev (:8000)
uv run ruff check .                               # lint — clean
uv run pytest -q                                  # 127 passed (hermetic)
python -m tests.fixture_eval                       # 67/67, all metrics 1.0
python -m tests.adversarial_eval                   # NL-gap proxy diagnostic
```

`tests/` covers: contract roundtrip, restart persistence, concurrent
sessions, malformed input, auth, extraction, evolution, recall/search,
LLM fallback + hermetic isolation, recall-synonym + false-positive
guards, and the deterministic fixture self-eval. **Internal proxy
metrics (not the private eval):** `fixture_eval` 67/67 with all metrics
= 1.0; `adversarial_eval` recall proxy 0.9355, noise/scope guardrails
1.0 in both no-key and mocked-LLM modes, `semantic_gap` 0.0.

## 14. Evaluation evidence (internal, committed)

Per-phase QA reports under `reports/`:
`pre_phase7_readiness.md`, `phase5_100_case_qa.md`,
`phase5_200_case_qa.md`, `phase6_fixtures_selfeval_qa.md`,
`phase7_llm_extraction_qa.md`, `phase7_1_recall_synonyms_qa.md`,
`phase8_final_verification.md`. `CHANGELOG.md` is the iteration history
(what changed / why / result / next, with metrics per step). All are
internal proxy evidence — not the Higgsfield private eval.

## 15. Configuration

`.env` is **optional** and gitignored (only `.env.example` is tracked);
the service runs with none. Recognised env vars (case-insensitive, no
prefix): `APP_NAME`, `LOG_LEVEL`, `DATA_DIR` (default `/data`),
`MEMORY_AUTH_TOKEN` (optional bearer), and the optional-LLM knobs
`LLM_ENABLED` (default `false`), `LLM_PROVIDER` (`openai`),
`OPENAI_API_KEY`, `OPENAI_MODEL` (default `gpt-5.2`),
`LLM_TIMEOUT_SECONDS`, `LLM_MAX_CANDIDATES`, `LLM_CONFIDENCE_CAP`,
`LLM_MAX_OUTPUT_TOKENS`. See `.env.example`.

## 16. Submission checklist

- [x] `docker compose up` boots with no manual steps; port 8080
- [x] Persists across `docker compose down && up` (named volume `/data`)
- [x] Exact HTTP contract (shapes/status codes; `extra=forbid` → 422)
- [x] Structured typed memories with provenance + supersession (not chunks)
- [x] Hybrid recall (not vanilla cosine); noise → empty; budgeted prose
- [x] Synchronous read-your-write; no DB txn across the LLM call
- [x] Optional LLM extraction, default-off, key-free fallback; hermetic tests
- [x] `ruff` clean · `pytest` 127 passed · self-eval 67/67 (1.0)
- [x] `README.md` (this) + Task.md-grade `CHANGELOG.md`
- [x] No secrets / `.env` / `*.db` committed; no AI/co-author attribution
