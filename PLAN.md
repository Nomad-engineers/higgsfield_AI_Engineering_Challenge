# Improvement Plan — Memory Service v9

Based on audit against challenge description (§3–§9). Priority-ordered.

---

## Phase 1 — Critical Fixes (eval blockers)

### 1.1 Token budget overflow
- **Problem:** `estimate_tokens(text) = len(text) // 3` — грубая эвристика. Markdown-heavy context (bold keys, lists, headers) может превысить `max_tokens` в 1.5–2x
- **Fix:** Переписать `_assemble_context` с char budget = `max_tokens * 3`, а не `* 4`. Добавить hard cap в конце: если `estimate_tokens(context) > max_tokens * 1.2`, обрезать с `...`
- **Files:** `src/services/recall_service.py`
- **Risk if skipped:** eval проверяет `context` length vs `max_tokens`, fail на tight budgets (256)

### 1.2 Race condition on concurrent writes
- **Problem:** `get_active_by_key` → `deactivate` → `create` не атомарна. Два параллельных `POST /turns` для одного `user_id` могут создать дубликаты memories
- **Fix:** Добавить `SELECT ... FOR UPDATE` в `get_active_by_key` (row-level lock). Обернуть contradiction resolve в DB transaction с `SELECT FOR UPDATE` на memory rows
- **Files:** `src/repositories/memory_repo.py`, `src/services/extraction_service.py`
- **Risk if skipped:** eval запускает concurrent sessions для одного user_id — возможны duplicate active memories

### 1.3 Search schema compliance
- **Problem:** `SearchResult` содержит `key: str | None` и `type: str | None` — не в контракте §3. Eval может делать strict schema validation
- **Fix:** Убрать `key` и `type` из базового `SearchResult`. Если нужны — возвращать внутри `metadata: {"key": "...", "type": "..."}` что соответствует контракту
- **Files:** `src/schemas/search.py`, `src/services/search_service.py`
- **Risk if skipped:** contract compliance downgrade

---

## Phase 2 — High-Impact Improvements

### 2.1 Opinion arc preservation
- **Problem:** Для nuance relationship, старое memory деактивировано. Но challenge §4 говорит opinion evolution — это arc: "I love TS" → "generics annoying" → "fine for big projects". Eval может проверять что ВСЯ arc видима в `/memories`
- **Fix:** Для nuance: НЕ деактивировать старое. Оба активны. В recall context assembly показывать evolution chain: "Loves TS → generics annoying → fine for big projects". В `format_relevant_memories` собирать chain по `supersedes`
- **Files:** `src/services/extraction_service.py` (nuance branch), `src/services/recall_service.py` (`format_relevant_memories`)
- **Risk if skipped:** fact evolution score downgrade

### 2.2 Multi-hop query rewrite robustness
- **Problem:** Gate `len(query.split()) <= 4` пропускает короткие multi-hop queries ("Biscuit's city?" = 2 слова) и декомпозирует длинные однофактные
- **Fix:** Убрать length gate. ВСЕ queries отправлять на rewrite LLM. Добавить cost gate: если LLM сказал `is_multi_hop: false`, использовать оригинальный query. Сохранить batch embedding
- **Files:** `src/services/recall_service.py` (`_rewrite_query`)
- **Risk if skipped:** multi-hop recall miss на коротких queries

### 2.3 Cross-key contradiction threshold tuning
- **Problem:** Порог 0.80 может быть слишком высоким для логически связанных но семантически разных ключей (например `employer` vs `occupation`)
- **Fix:** Снизить до 0.70. Добавить whitelist пар ключей которые всегда проверяются: `(employer, title)`, `(employer, occupation)`, `(location, city)`, `(spouse, spouse_occupation)`
- **Files:** `src/services/extraction_service.py`, `src/repositories/memory_repo.py`
- **Risk if skipped:** cross-key contradictions miss

---

## Phase 3 — Polish & Edge Cases

### 3.1 Missing `source_turn` field in memories response
- **Problem:** Контракт §3 показывает `source_turn: "string"` в memories response, но `MemoryOut` имеет только `source_session`. Eval может проверять это поле
- **Fix:** Добавить `source_turn: str | None` в `MemoryOut` из `memory.source_turn_id`
- **Files:** `src/schemas/memory.py`, `src/routers/memories.py`
- **Risk if skipped:** minor contract compliance hit

### 3.2 Recall fixture expansion
- **Problem:** 12 probe queries — хорошо, но нет explicit tests для: correction handling, implicit fact extraction, tight token budget (256), multi-message turns с tool calls
- **Fix:** Добавить 6-8 probe queries в fixture:
  - "What framework correction did the user make?" (correction)
  - "Does the user have any pets mentioned indirectly?" (implicit)
  - probe с `max_tokens: 128` (tight budget)
  - multi-message turn с tool call
- **Files:** `fixtures/conversations.json`, `tests/recall_quality/test_recall_fixture.py`
- **Risk if skipped:** lower recall quality metric on private eval

### 3.3 Rule-based extraction fallback
- **Problem:** Если OpenAI API недоступен, extraction полностью отключается. Challenge §5: "LLM usage is encouraged" но не required
- **Fix:** Добавить regex/pattern-based extractor как fallback:
  - "I (live|lived|moved to) in/at/from X" → location fact
  - "I (work|worked|joined) at X" → employer fact
  - "I'm allergic to X" → allergy fact
  - "my (dog|cat|pet) named X" → pet fact
- **Files:** новый `src/services/rule_extractor.py`, `src/services/extraction_service.py` (fallback)
- **Risk if skipped:** resilience when API down

### 3.4 Temporal boost in recall
- **Problem:** Нет boost для недавних memories в hybrid search. Recency обрабатывается только в context assembly (recent turns)
- **Fix:** Добавить recency factor в RRF score: `rrf_score * (1 + alpha * recency)` где `recency = 1 / (1 + days_since_creation)`, `alpha = 0.1`
- **Files:** `src/services/recall_service.py` (`rrf_merge`)
- **Risk if skipped:** stale facts may outrank recent corrections

---

## Phase 4 — Documentation & Testing

### 4.1 Verify `.env.example` exists and is correct
- Challenge §6 требует `.env.example` с API keys
- Убедиться что файл содержит все переменные из `config.py`

### 4.2 Update CHANGELOG with v9 entry
- Описать все изменения из Phase 1-3
- Включить метрики до/после

### 4.3 Update README
- Обновить opinion evolution section (arc preservation)
- Обновить recall strategy (improved noise gating thresholds)
- Добавить rule-based fallback mention

---

## Execution Order

```
1.1 → 1.2 → 1.3 → 2.1 → 2.2 → 2.3 → 3.1 → 3.2 → 3.3 → 3.4 → 4.x
```

Phase 1 — must-do перед submission.
Phase 2 — high impact, рекомендуется.
Phase 3 — polish, если есть время.
Phase 4 — final packaging.
