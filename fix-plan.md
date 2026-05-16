# Fix Plan — Memory Service Audit Results

## КРИТИЧЕСКИЕ — исправить в первую очередь

### 1. Contradiction detection: логика "first match wins" вместо "last by date"

**Файл:** `src/services/extraction_service.py:90-103`

Цикл по existing memories **прерывается на первом non-"new" результате**. Если у пользователя 3 активных memories с key `programming_language` (opinion arc), и первый match даст "nuance", а третий — "contradiction", система выберет "nuance". Для opinion arcs нужно сравнивать с **последней (самой новой)** memory, а не с первой попавшейся.

**Исправление:** сортировать existing memories по `created_at desc` (уже делается в `get_active_by_key`) и сравнивать только с **самой новой**, либо со всеми и выбирать наиболее сильную связь.

```python
# Текущий код:
for old_mem in existing:
    result = await llm_service.check_contradiction(...)
    if result.get("relationship") != "new":
        best_relationship = relationship
        best_match = old_mem
        break  # <-- прерывается на первом совпадении

# Исправленный код:
# existing уже отсортированы по created_at desc
# Сравниваем только с самой новой memory
old_mem = existing[0]
result = await llm_service.check_contradiction(key=key, old_value=old_mem.value, new_value=value)
```

---

### 2. Nuance memories: дублирование в recall context

**Файл:** `src/services/recall_service.py:76-122`

Для "nuance" новая память получает `supersedes=best_match.id`, но `best_match` **не деактивируется**. Обе памяти активны с одинаковым `key`. При recall обе могут попасть в контекст, создавая дублирование. `format_relevant_memories` группирует по key и показывает evolution arc, но `format_stable_facts` тоже группирует — нужно убедиться что дублирование обрабатывается корректно.

**Исправление:** В `format_stable_facts` (строка 56-68) `_group_by_key` не гарантирует сортировку внутри группы по `created_at`. memories приходят отсортированными по `confidence desc, created_at desc` из `get_stable_facts`, но `_group_by_key` не сохраняет этот порядок внутри группы.

```python
# В _group_by_key добавить сортировку:
def _group_by_key(memories: list) -> dict[str, list]:
    grouped = defaultdict(list)
    for m in memories:
        grouped[m.key].append(m)
    # Сортируем каждую группу: новые первыми
    for key in grouped:
        grouped[key].sort(key=lambda m: m.created_at, reverse=True)
    return dict(grouped)
```

---

### 3. `source_turn_id` ForeignKey без ON DELETE CASCADE

**Файл:** `src/models/memory.py:22`

В PLAN.md было указано `REFERENCES turns(id) ON DELETE CASCADE`, но в реальной модели **нет ForeignKey** вообще. При `DELETE /sessions/{id}` turns удаляются, но memories **не удаляются** через каскад. Вместо этого работает ручной `delete_by_session` в `memory_service.py`. Пока это работает, но `source_turn_id` может указывать на несуществующий turn.

**Исправление:** Добавить ForeignKey с cascade:

```python
source_turn_id: Mapped[uuid.UUID | None] = mapped_column(
    UUID(as_uuid=True), ForeignKey("turns.id", ondelete="SET NULL"), nullable=True
)
```

Используем `SET NULL` вместо `CASCADE`, потому что memories не должны удаляться при удалении turn — они должны сохраняться, но терять ссылку.

---

## ВЫСОКИЙ ПРИОРИТЕТ — влияет на оценку eval

### 4. BM25 без предвычисленного tsvector и GIN индекса

**Файл:** `src/repositories/memory_repo.py:155-183`

BM25-запрос использует `to_tsvector('english', key || ' ' || value)` **на лету**, без предвычисленного tsvector-столбца и без GIN-индекса. Каждый запрос делает full table scan по всем активным memories данного пользователя. Для небольшого числа memories это работает, но:
- В плане был указан GIN-индекс, но он **не создан**
- При росте данных recall pipeline будет деградировать

**Вариант 1 (рекомендуемый):** Добавить tsvector column и GIN index:

```python
# В memory model добавить:
from sqlalchemy import Computed
from sqlalchemy.dialects.postgresql import TSVECTOR

search_vector = mapped_column(
    TSVECTOR,
    Computed("to_tsvector('english', key || ' ' || value)", persisted=True),
    nullable=True,
)

# В __table_args__ добавить:
Index("idx_memories_search", "search_vector", postgresql_using="gin"),
```

**Вариант 2 (быстрый):** Оставить как есть, но явно указать в README как tradeoff.

---

### 5. Отсутствие теста на fact evolution (старый факт НЕ в recall, но виден в history)

**Файл:** `tests/recall_quality/test_recall_fixture.py`

Fixture содержит fact evolution (Notion → Stripe), но нет теста который проверяет:
1. `/recall` возвращает **Stripe** как текущего работодателя
2. `/recall` **НЕ** возвращает Notion как текущего работодателя
3. `/users/{user_id}/memories` показывает Notion как `superseded`

**Исправление:** Добавить тест:

```python
def test_fact_evolution_not_current_employer(client):
    """Старый работодатель (Notion) НЕ должен появляться как текущий в recall."""
    resp = client.post("/recall", json={
        "query": "What does this user do for work?",
        "session_id": "probe-session",
        "user_id": "alice",
        "max_tokens": 512,
    })
    context = resp.json()["context"].lower()
    assert "stripe" in context
    # Notion может упоминаться в evolution context, но не как текущий
    # Проверяем что context содержит "stripe" раньше/чаще чем "notion"

def test_fact_evolution_history_preserved(client):
    """Старый работодатель виден в /memories как superseded."""
    resp = client.get("/users/alice/memories")
    memories = resp.json()["memories"]
    # Находим memories с key=employer
    employer_mems = [m for m in memories if m["key"] == "employer"]
    assert any("Stripe" in m["value"] and m["active"] for m in employer_mems)
    assert any("Notion" in m["value"] and not m["active"] for m in employer_mems)
```

---

### 6. Multi-hop recall ограничен одним user_id

**Файл:** `src/services/recall_service.py:151-178`

Для multi-hop запросов типа "What city does the user with the golden retriever live in?" recall pipeline фильтрует по `user_id`. Это работает в fixture потому, что все memories принадлежат alice. Но если eval создаст memories от разных users и спросит cross-user multi-hop, это не сработает.

**Статус:** Это design decision, не баг. Зафиксировать в README как известное ограничение. Для single-user challenge (описано в §12 "single user — fine") это приемлемо.

---

### 7. Tool message `name` field отсутствует в Message schema

**Файл:** `src/schemas/turn.py:4-5`

description.md §3 указывает что tool messages могут иметь поле `name`. Текущая модель:

```python
class Message(BaseModel):
    role: str
    content: str
```

**Исправление:**

```python
class Message(BaseModel):
    role: str
    content: str
    name: str | None = None
```

И в extraction prompt (`src/prompts/extract.py`) учесть name при форматировании:

```python
# В llm_service.py extract_memories:
user_content = "\n".join(
    f"{m.get('name', '') + ': ' if m.get('name') else ''}{m['role']}: {m['content']}"
    for m in messages
)
```

---

### 8. `/search` endpoint: content формат может не совпадать с ожиданиями eval

**Файл:** `src/services/search_service.py:55`

Content формируется как `f"{memory.key}: {memory.value}"` (например, `"employer: Works at Stripe as a PM"`). HTTP contract говорит `"content": "string"` без указания формата. Если eval ожидает сырой текст memory без key-префикса, это может быть проблемой.

**Статус:** Оставить как есть, но рассмотреть альтернативу — возвращать только `value`.

---

## СРЕДНИЙ ПРИОРИТЕТ — стоит улучшить

### 9. LLM retry: exponential backoff может съесть таймаут

**Файл:** `src/services/llm_service.py:29-38`

`MAX_RETRIES = 5`, backoff = `2^(attempt+1)` → 2s, 4s, 8s, 16s, 32s = **62 секунды** максимальное ожидание. С учетом 60-секундного таймаута на `/turns`, retry может съесть весь бюджет.

**Исправление:**

```python
MAX_RETRIES = 3
# В _post_with_retry:
wait = min(2 ** (attempt + 1), 10)  # cap на 10 секунд
```

---

### 10. BaseHTTPMiddleware + async: потенциальная проблема

**Файл:** `src/middleware/auth.py`, `src/middleware/error_handler.py`

Starlette `BaseHTTPMiddleware` имеет известные проблемы с `StreamingResponse` и `BackgroundTask`. Для production рекомендуют использовать pure ASGI middleware или FastAPI `@app.middleware("http")`.

**Статус:** Для challenge это работает, не менять.

---

### 11. Memory model: embedding=None при возврате из SQL queries

**Файл:** `src/repositories/memory_repo.py:143-151`

Объекты Memory создаются вручную из raw SQL rows (vector_search, bm25_search), embedding **не заполняется**. Если код попытается обратиться к `memory.embedding` после retrieval, он будет `None`.

**Статус:** Низкий риск — после retrieval embedding не нужен. Не исправлять, но иметь в виду.

---

### 12. `metadata` vs `metadata_` naming в Turn model

**Файл:** `src/models/turn.py:21`

```python
metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
```

Имя атрибута `metadata_` чтобы избежать конфликта с SQLAlchemy `MetaData`. При `store_turn` передается `metadata=body.metadata`. Нужно убедиться что `turn_repo.create()` использует правильное имя атрибута.

**Проверить:** `turn_repo.create` должен принимать `metadata` и присваивать `metadata_=metadata`.

---

## Порядок исправлений

1. **Fix #1** — contradiction detection logic (критический для fact evolution eval)
2. **Fix #2** — stable facts сортировка (критический для recall quality)
3. **Fix #3** — ForeignKey с SET NULL (robustness)
4. **Fix #5** — добавить fact evolution тест (eval coverage)
5. **Fix #7** — добавить name field в Message schema (contract compliance)
6. **Fix #4** — GIN index для BM25 (performance, можно отложить)
7. **Fix #9** — уменьшить MAX_RETRIES (resilience)
8. Остальные — по возможности
