# Changelog

Iteration history. One entry per significant design iteration, each with
**What changed · Why · Result · Next**. All metrics are from the
project's **internal proxy evals** (`tests/fixture_eval.py`,
`tests/adversarial_eval.py`, the per-phase 200-case QA reports) — they
are deterministic regression instruments, **not** the Higgsfield private
eval. Early structural phases report only what was actually measured (no
fabricated numbers).

## v1 — Repository reshape (Phase 1)

**What changed:** Replaced the generic `app/` job-queue scaffold with the
challenge's `src/memory_service/` layout (api / schemas / core / storage
/ extraction / recall), src-layout `pyproject.toml` (hatchling; dropped
celery/redis), `Dockerfile` + `docker-compose.yml` (port 8080, named
volume at `/data`), import-safe stubs.

**Why:** The synchronous memory contract shares no logic with the old
async `/jobs`; adapting it would carry dead weight. Simplest correct
shape for the contract.

**Result:** `ruff` clean · `pytest` green (import-safe stub) ·
`/architecture-review` pass (no over-engineering; SQLite + named volume
sane). No quantitative recall metric yet (structural phase).

**Next:** Implement the exact HTTP contract + SQLite foundation.

## v2 — Contract endpoints + SQLite foundation (Phase 2)

**What changed:** All 7 endpoints with exact shapes/status codes,
Pydantic request models with `extra="forbid"`; SQLite foundation — WAL,
`foreign_keys=ON`, single connection + write lock, short transactions,
full schema (turns/messages/memories/memory_entities + FTS5 probed/
created), optional bearer auth, scoped deletes. Memory tables created but
unpopulated; recall/search/memories return valid empty shapes.

**Why:** Lock the contract and persistence first; design the whole
schema once (the DB is volume-persisted — no migration story).

**Result:** contract-roundtrip, restart-persistence, concurrent-session
isolation and auth tests pass; `ruff` clean; `/fastapi-review` pass
(thin routes, exact contract).

**Next:** Turn raw turns into structured typed memories.

## v3 — Deterministic rule-based extraction (Phase 3)

**What changed:** Pure regex extraction (employment, location, pets,
allergies, diet, communication, opinions/preferences, events) with
strict subject gates (first-person personal facts; relaxed-but-
attribution-suppressed opinions; role gate — assistant/tool text never
extracted), canonical keys, scope assignment, intra-payload last-wins
corrections.

**Why:** A deterministic, key-free, testable baseline that produces
**structured typed memories, not chunks** — precision over recall.

**Result:** `test_extraction` passes; a false-positive control set
("my friend works at…") confirmed third-party leakage is suppressed;
`ruff` clean; `/memory-quality-review` pass. Coverage is intentionally
precision-first (implicit/paraphrase deferred).

**Next:** Detect contradictions and evolve facts over time.

## v4 — Fact evolution / supersession (Phase 4)

**What changed:** Mutable vs append-only canonical keys; a new differing
value supersedes the prior active row (`active=0` + `supersedes`),
append-only values coexist, exact re-affirmations dedupe; self-FK
delete-safety (null inbound pointers before deleting a referenced row).

**Why:** The contract rewards detected contradictions with preserved
history — never delete on update.

**Result:** `test_evolution` passes (Stripe→Notion, Berlin→Paris,
cross-session chains, delete-safety); `ruff` clean;
`/memory-quality-review` pass. A cross-session supersede chain broke
`DELETE /sessions/{id}` with an FK error until inbound pointers were
nulled in the same short transaction — fixed.

**Next:** Build real hybrid recall + search.

## v5 — Hybrid recall + search + QA hardening (Phase 5, `0ed62f8`)

**What changed:** Real `/recall` (budgeted readable prose + deduped
citations) and `/search` (ranked structured results) over a deliberate
non-vanilla hybrid: structured canonical/entity + FTS5/BM25 (+LIKE
fallback) + 1-hop multi-hop, fused with active/confidence/recency/
same-session boosts, current-vs-history intent, noise gate (empty on
cold, never 500 on FTS-hostile input).

**Why:** "Vanilla cosine-top-k will not score" — recall must be
deliberate, debuggable, and reproducible.

**Result:** internal 200-case QA proxy **PASS 191 / PARTIAL 10 / FAIL
0**; **80 pytest** passing; `ruff` clean. The audit drove correction-
phrasing fixes and recall-paraphrase coverage; embeddings deferred
(structured+lexical+multi-hop already beats vanilla cosine,
pre-measurement).

**Next:** A repeatable self-eval so every later change is measured.

## v6 — Fixtures + deterministic self-eval (Phase 6, `1c3ce99`)

**What changed:** A repeatable, task-focused self-eval —
`fixtures/conversations.json` (15 scripted conversations, canonically
phrased so extraction ground-truth is exact), `fixtures/probes.json`
(67 probes with dense `must_not` + per-probe `gated`), pure-HTTP runner
`tests/fixture_eval.py`, pytest gate, `tests/regression_bank.json`.
Deterministic grading (no LLM-as-judge); no product code changed.

**Why:** Make recall/extraction quality measurable and regression-proof
— the iteration loop for all later phases.

**Result:** `fixture_eval` **67/67, all metrics 1.0, COMMIT-OK True**;
internal Phase-6 200-case QA **200 PASS / 0 PARTIAL / 0 FAIL**;
**94 pytest**; `ruff` clean. The self-eval surfaced one real gap
("employed" not in the recall hint vocab) → fixed with a one-token
`_EMP \w*` hardening (a hint only boosts in-scope memories — noise gate
still prevents fabrication).

**Next:** Measure the real natural-language gap before adding any LLM.

## v6.5 — Pre-Phase-7 readiness (`20709a1`)

**What changed:** Live Docker/persistence smoke (isolated compose
project) + a dev/test-only adversarial NL-gap diagnostic
(`tests/adversarial_eval.py`, ~100 cases) that attributes every miss to
extraction-gap / recall-vocab / recall-lexical / semantic / acceptable-
limit. No product code changed.

**Why:** Decide Phase 7 (LLM) and Phase 7.5 (embeddings) from data, not
assumption.

**Result:** Docker smoke **GREEN**, named-volume persistence proven;
internal adversarial proxy — extraction **0.14**, recall **0.71**,
implicit **0.0**, **`semantic_gap = 0.0`**; **99 pytest**; `ruff` clean;
`fixture_eval` unchanged (67/67, repeatability invariant). Two earlier
container-name/port collisions identified as a local parallel-run smell
(not a clean-host failure).

**Next:** LLM extraction is measured-justified; embeddings deferred
(`semantic_gap = 0.0`).

## v7 — Optional LLM extraction (Phase 7, `095c94a`)

**What changed:** Optional, **default-OFF**, failure-tolerant LLM
extraction arm behind a provider seam (`extraction/llm_extractor.py`):
strict typed `LLMCandidate` schema, two-mode grounding (direct span +
small allowlisted implicit map), third-party suppression, confidence
cap, rules-authoritative `merge_candidates`, **no DB txn across the LLM
call**. Provider = already-locked OpenAI SDK (no new dependency);
Gemini-via-httpx seam-ready; Vertex rejected. Test suite made hermetic.

**Why:** Measured rule-only NL extraction ≈ 0.14 — the dominant
lost-points risk — while the service must still run with zero
credentials.

**Result:** **116 pytest** (hermetic — no real API even with a local
`.env`/key, proven by `tests/test_llm_isolation.py`); `fixture_eval`
**67/67 unchanged** (LLM-disabled identity invariant); internal
adversarial extraction proxy **no-key 0.14 → mocked-LLM 0.76**, implicit
**0.0 → 0.70**, guardrails noise/scope **1.0** in both modes;
internal Phase-7 200-case QA **198 PASS / 2 PARTIAL / 0 FAIL** (the 2
PARTIALs are documented strict-grounding limits — non-span semantic
remaps refused → zero ungrounded hallucination). Optional manual
real-provider smoke passed (repeatability 3/3) — **not** part of the
mandatory QA. `semantic_gap` re-measured **0.0**.

**Next:** Close the measured recall vocab/lexical gaps deterministically;
keep embeddings deferred.

## v7.1 — Measured recall synonym cleanup (Phase 7.1, `ebd8e0b`)

**What changed:** Tiny guarded widening of the `recall/query.py` hint
regexes only — plain `workplace,salary` (`_EMP`), `cuisine` (`_DIET`),
`tone,chatty` (`_COMM`); **guarded** `(?:preferred|response|reply|
answer|communication)\s+format` (`_COMM`) and `(?:food|dietary|medical)
\s+sensitiv\w*` / `sensitiv\w*\s+to` (`_AVOID`). `hometown` was
**deliberately excluded** (origin ≠ current city → would be a false
fact). No logic/extraction/LLM/embeddings/dependency change.

**Why:** The Pre-Phase-7 diagnostic showed the recall misses were
lexical/vocabulary (`semantic_gap = 0.0`) — closable deterministically,
no embeddings needed.

**Result:** **127 pytest** (incl. 11 new positive + false-positive
guard tests); `fixture_eval` **67/67 unchanged**; **internal adversarial
recall proxy 0.7097 → 0.9355** (vocab gap 7→1, lexical 2→1); internal
Phase-7.1 200-case QA **200 PASS / 0 PARTIAL / 0 FAIL**; guardrails
noise/scope **1.0** in both modes; extraction (0.14/0.76) and
`semantic_gap` (0.0) unchanged. Residual 1+1 = two **documented
ambiguity limits** (`hometown`; "for a living") — return nothing rather
than a wrong fact.

**Next:** Final submission polish (README, CHANGELOG, compose cleanup,
final verification).

## v8 — Final submission polish (Phase 8)

**What changed:** Final, accurate `README.md` (16 reviewer-friendly
sections covering Task.md's 8 must-haves: architecture diagram + prose,
backing-store rationale, extraction pipeline incl. what it misses,
recall strategy + token-budget priority, fact evolution, tradeoffs,
failure modes, how to run tests); this Task.md-grade CHANGELOG rewrite
(every entry **What changed / Why / Result / Next** with real proxy
metrics); and the single justified config cleanup — **removed the
hardcoded `container_name`** from `docker-compose.yml` (service name,
port, healthcheck, named-volume semantics unchanged). No product/
extraction/recall/LLM/embeddings/dependency change.

**Why:** The CHANGELOG and README are the most-weighted human-review
deliverables; they must be accurate to the code, label internal evals
as proxies (not the private eval), and not overclaim. The fixed
container name was a parallel-run reproducibility smell (global Docker
names) — not a clean-host failure, but worth removing.

**Result:** `ruff` clean · `pytest -q` **127 passed** (hermetic) ·
`fixture_eval` **67/67, all metrics 1.0**, COMMIT-OK True ·
`adversarial_eval` recall proxy **0.9355**, extraction no-key **0.14** /
mocked-LLM **0.76**, `semantic_gap` **0.0**, guardrails noise/scope
**1.0** in both modes (no regression vs v7.1). Isolated Docker smoke
**GREEN** with the name removed — compose auto-named
`memory-phase8-smoke-memory-service-1`, health/turns/recall/users OK,
**persistence proven** (a recall in a *new* session after `down`→`up`
returned the same fact and the same `turn_id`), healthcheck/port/volume
unaffected. Phase-8 final 200-case QA **200 PASS / 0 PARTIAL / 0 FAIL**
(API contract, restart-persistence, no-regression, extraction/evolution/
correction, recall/search incl. synonym+history+multi-hop, scope, auth,
LLM default-off/hermetic, delete-safety, README/CHANGELOG structure,
secrets hygiene). Hygiene scan clean: no `.env`/`*.db` tracked, no API
key or AI/co-author attribution in code/docs/git history,
`reports/current_phase_handoff.md` untracked.

**Next:** Submit. Open optional follow-ups (not blockers): a measured
local-embedding RRF arm only if a future true-semantic gap is observed
(currently 0.0); broader implicit-fact extraction via the (default-off)
LLM arm.
