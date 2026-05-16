# Оценка HYBRID_PLAN.md

> Дата: 2026-05-16
> Контекст: оценка плана гибридизации (rules + LLM) против текущей кодовой базы и требований челленджа

---

## Резюме

HYBRID_PLAN.md — грамотный план улучшений, который правильно диагностирует слабые места текущего решения, но содержит ряд критических несоответствий между планом и реальностью кодовой базы, а также несколько спорных архитектурных решений.

**Общая оценка: 7/10**

---

## 1. Что план правильно диагностирует (плюсы)

| Проблема | Гипотеза | Оценка |
|---|---|---|
| **`extra="forbid"` на Pydantic-схемах** | Остро нужно — текущие схемы молча игнорируют неизвестные поля | Верное решение. Высокий приоритет. |
| **LLM-only extraction** | Без API-ключа сервис не создаёт memories — `extraction_service.py:41` падает в `except`, fallback на rules, но rules дают 0 результатов при пустых матчах | Верное. План определяет, что rules должны быть **первичным** путём. |
| **DB txn across LLM** | `_resolve_memory` держит `for_update=True` в `memory_repo.get_active_by_key` и затем делает LLM-вызов | Верно. Это реальный баг — `SELECT FOR UPDATE` + network I/O = deadlock risk. |
| **Canonical keys** | Свободные LLM-ключи (`"employer"`, `"location"`) делают supersession хрупкой | Верно. Ключи от LLM не нормализованы → `"employer"` ≠ `"company"` ≠ `"workplace"` = дубли. |
| **Noise resistance** | Текущий `_assemble_context` неплох, но нет density gate | Верное улучшение. |
| **Test coverage** | Текущий набор тестов мал, требует расширения | Верно. |

---

## 2. Спорные решения (риски)

### 2.1 `LLM_ENABLED=false` по умолчанию — серьёзный стратегический риск

План предлагает: "zero-setup, no API key → rules-only extraction". Это значит:

- На eval (где дадут API-ключ) вы по умолчанию используете rules-only, а не LLM
- Но rules — это 6 regex-паттернов в `rule_extractor.py`. Они ловят `"I live in X"`, `"I work at X"`, `"my dog named X"` и ещё ~4 паттерна
- Челлендж говорит: *"recognize and extract implicit facts ('walking Biscuit this morning' → has a pet named Biscuit)"* — regex это не умеет
- Челлендж говорит: *"extraction pipeline: How you turn raw conversation turns into structured, queryable knowledge. LLM-based, rule-based, NLP, hybrid — your call"*

**Вердикт:** Rules-only = провал extraction quality на eval. LLM extraction должен быть включён по умолчанию при наличии ключа. `LLM_ENABLED` не нужен — достаточно проверять `OPENAI_API_KEY`.

### 2.2 "Rules authoritative on conflicts" — спорно

Если rules извлекли `"employer": "Stripe"` и LLM извлёк `"employer": "Stripe, started March 2025"`, план заставляет брать правило. Но LLM-версия богаче. Зачем терять информацию?

Лучше: rules и LLM заполняют разные слоты (canonical keys). Конфликт по одному ключу → брать LLM с grounding-проверкой, а не слепо rules.

### 2.3 Canonical keys — overengineering

План вводит `CANONICAL_MAP` из ~30 записей (`"employer" → "employment.company"`, `"city" → "location.current_city"`). Это:

- Хрупкий mapping, который нужно поддерживать
- Не покрывает все возможные ключи (LLM может вернуть `"favorite_framework"`)
- Не нужен если LLM возвращает structured output с `key` как controlled vocabulary через prompt engineering

**Альтернатива:** Заставить LLM использовать нормализованные ключи через extraction prompt + validation. Проще и мощнее.

### 2.4 Append-only vs Mutable keys — правильно, но не ново

Текущий `evolution.py` уже реализует supersession через `check_contradiction` LLM-вызов. План заменяет его на статическую классификацию ключей (`MUTABLE_KEYS` / `APPEND_ONLY_KEYS`). Это лучше для determinism, но хуже для nuanced случаев:

- `"opinion"` — mutable, но `"opinion"` может быть разных топиков (opinion about Python ≠ opinion about Go). Текущий LLM-подход обрабатывает это через `relationship` classification.
- Статическая классификация + LLM fallback для cross-key = два механизма для одной задачи

---

## 3. Критические несоответствия план ↔ код

| В плане | В реальности | Проблема |
|---|---|---|
| "Rule baseline, always-on" | `RuleExtractor` — 6 regex, извлекает из всех messages (не только user) | `rule_extractor.py:63` — нет фильтра `role == "user"`. План обещает subject gates, но их нет. |
| "Canonical keys" | LLM возвращает свободные ключи, нет нормализации | В `extraction_service.py` нет `canonicalize.py` |
| "No DB txn across LLM" | `_resolve_memory` вызывает `get_active_by_key(for_update=True)` → LLM call → update | План обещает "commit before LLM", но не показывает как именно разорвать транзакцию |
| "120+ hermetic tests" | Сейчас ~33 тестов, часть требует Docker | Нет `conftest.py` с `os.environ["LLM_ENABLED"] = "false"` |
| "Cross-key contradiction via pgvector > 0.80" | Уже реализовано: `CROSS_KEY_SIMILARITY_THRESHOLD = 0.70` | План предлагает 0.80 — minor diff, но не документирован |
| "67+ probes" | `fixtures/conversations.json` существует, но `probes.json` — нет | |
| "extra=forbid" на всех schemas | Нет ни на одной схеме | Критический miss |

---

## 4. Что план упускает

1. **Recall без embeddings** — план говорит "BM25 + canonical keys work without embeddings". Но `recall_service.py:199` при ошибке embedding падает в `_fallback_recall`, который просто возвращает последние 5 memories без relevance. Нет BM25-only recall pipeline.

2. **`/search` endpoint** — план не упоминает `search_service.py`. Этот endpoint в челлендже — "explicit search invoked by agent tool call". Он должен работать параллельно с `/recall`.

3. **Timestamp handling** — схемы используют `str` для timestamp, а не `datetime`. Челлендж говорит `"timestamp":"ISO-8601 string"`. Работает, но хрупко.

4. **Opinion evolution arc** — челлендж конкретно требует: *"I love TypeScript → TypeScript generics are getting annoying → TypeScript is fine for big projects"*. Текущий код и план не моделируют arcs. Просто "latest stance per topic" — не arc.

5. **`DELETE /sessions/{session_id}` и `DELETE /users/{user_id}`** — план не описывает cleanup logic. Эти endpoints критичны для eval (cleanup between scenarios).

---

## 5. Оценка по шкале "Excellent" из челленджа

| Критерий | Текущий статус | Что план даёт | После плана |
|---|---|---|---|
| Contract compliance | ~70% (no extra=forbid, shapes ~ok) | +extra=forbid, schema fixes | ~90% |
| Structured memories | ~60% (LLM-only, free-form keys) | +canonical keys, +rules | ~75% |
| Fact evolution | ~70% (LLM contradiction works, but fragile) | +mutable/append-only, +cross-key | ~80% |
| Recall ranking | ~80% (RRF + rerank + query rewrite — good!) | +density gate, +noise floor | ~85% |
| Token budget | ~75% (priority logic exists) | +density gate redistribution | ~80% |
| Persistence | ~80% (works, but untested restart) | +tests | ~90% |
| Zero-setup | 0% (requires API key) | +rules-only boot | ~70% |
| Tests | ~40% | +120 hermetic target | ~75% if implemented |
| CHANGELOG | ~60% (8 entries, decent) | +measurement discipline | ~80% |
| README | ~60% | +16 sections | ~80% |

---

## 6. Рекомендация: фокус на 5 улучшениях

Вместо полной гибридизации, сфокусируйтесь на 5 конкретных улучшениях к текущей кодовой базе:

### 1. `extra="forbid"` на всех Pydantic-схемах — 30 мин, высокий ROI

```python
class TurnCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # ...
```

### 2. Усилить `rule_extractor.py` — ~3h

- Добавить subject gate (`role == "user"` — только из сообщений пользователя)
- Расширить паттерны (diet, communication style, corrections, implicit facts)
- Добавить простую key normalization ( employer/company/workplace → employer )

### 3. Разорвать DB txn перед LLM — ~1h

- Commit turn → LLM extraction → second commit with memories
- Убрать `for_update=True` из `get_active_by_key` перед network call

### 4. Добавить BM25-only fallback recall — ~2h

- При отсутствии embeddings, `/recall` должен работать через BM25 + recency
- Сейчас падает в `_fallback_recall` = последние 5 memories без relevance

### 5. Расширить tests + fixtures — ~3h

- Hermetic conftest (force LLM off, drop API key)
- Contract tests для всех 7 endpoints
- Recall quality fixture с `must_match` / `must_not` probes

**Итого: ~10h работы, ~80% бенефита плана.**

Остальное (canonical key map, density gate, cross-key improvement) — nice-to-have, если останется время.

---

## 7. Вердикт

HYBRID_PLAN.md — хороший аналитический документ, но не готов к прямому исполнению. Основные проблемы:

1. **Overengineering** — canonical keys, LLM_ENABLED flag, dual extraction merge — это 3 отдельных механизма для одной задачи (structured extraction). Проще: хороший extraction prompt + validation.

2. **Неверный default** — LLM-off-by-default убьёт extraction quality на eval. Челлендж ожидает structured memories, не regex matches.

3. **23h estimate** — нереалистичен для оставшегося времени, учитывая что текущая кодовая база уже существует и нужно модифицировать, а не писать с нуля.

4. **Missing pieces** — `/search`, cleanup endpoints, opinion arcs, BM25-only recall — не покрыты.

**Путь вперёд:** использовать диагностику из плана (что сломано), но реализовывать минимальные точечные исправления, а не полную гибридизацию.
