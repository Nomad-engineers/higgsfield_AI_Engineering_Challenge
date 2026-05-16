# План реализации — 5 точечных улучшений

> Дата: 2026-05-16
> Основа: оценка из EVALUATION_HYBRID_PLAN.md
> Цель: ~80% бенефита полной гибридизации за ~10h работы

---

## Фаза 1: `extra="forbid"` на всех Pydantic-схемах

**Время:** ~30 мин
**Приоритет:** P0 — eval проверяет contract compliance
**ROI:** максимальный

### Что менять

**Файлы:** `src/schemas/turn.py`, `src/schemas/recall.py`, `src/schemas/search.py`, `src/schemas/memory.py`

```python
# src/schemas/turn.py
from pydantic import BaseModel, ConfigDict, Field

class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    content: str
    name: str | None = None

class TurnCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    user_id: str | None = None
    messages: list[Message] = Field(..., min_length=1)
    timestamp: str
    metadata: dict | None = None

class TurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
```

```python
# src/schemas/recall.py
from pydantic import BaseModel, ConfigDict

class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: str | None = None
    score: float
    snippet: str

class RecallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    session_id: str
    user_id: str | None = None
    max_tokens: int = 512

class RecallResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context: str
    citations: list[Citation]
```

```python
# src/schemas/search.py
from pydantic import BaseModel, ConfigDict

class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    session_id: str | None = None
    user_id: str | None = None
    limit: int = 10

class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str
    score: float
    session_id: str
    timestamp: str
    metadata: dict = {}

class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[SearchResult]
```

```python
# src/schemas/memory.py
from pydantic import BaseModel, ConfigDict

class MemoryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    type: str
    key: str
    value: str
    confidence: float
    active: bool
    source_session: str
    source_turn: str | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    created_at: str
    updated_at: str

class MemoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memories: list[MemoryOut]
```

### Тест

```python
# tests/contract/test_extra_forbid.py
import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_turns_rejects_unknown_fields(client: AsyncClient):
    resp = await client.post("/turns", json={
        "session_id": "s1",
        "messages": [{"role": "user", "content": "hi"}],
        "timestamp": "2025-01-01T00:00:00Z",
        "metadata": {},
        "unknown_field": "should_fail"
    })
    assert resp.status_code == 422

@pytest.mark.asyncio
async def test_recall_rejects_unknown_fields(client: AsyncClient):
    resp = await client.post("/recall", json={
        "query": "test",
        "session_id": "s1",
        "max_tokens": 512,
        "extra": "bad"
    })
    assert resp.status_code == 422
```

### Критерий готовности

- `docker compose up` → smoke test проходит
- Все 4 schema-файла содержат `extra="forbid"`
- Тест с неизвестным полем возвращает 422

---

## Фаза 2: Усиление `rule_extractor.py`

**Время:** ~3h
**Приоритет:** P0 — extraction fallback quality
**Проблема:** 6 regex-паттернов, нет фильтра по role, нет key normalization

### Что менять

**Файл:** `src/services/rule_extractor.py` — полный rewrite

#### 2.1 Subject gate — извлекать только из user-сообщений

```python
def extract(self, messages: list[dict]) -> list[dict]:
    results = []
    seen = set()

    for msg in messages:
        if msg.get("role") != "user":       # <-- SUBJECT GATE
            continue
        content = msg.get("content", "")
        if not content or not isinstance(content, str):
            continue
        # ... patterns ...
```

#### 2.2 Расширенные паттерны

Текущие 6 паттернов → ~15 категорий:

```python
PATTERNS = [
    # --- Location ---
    (re.compile(
        r"(?i)\bI\s+(?:live|am living|am based|reside|moved)\s+(?:in|to)\s+(.+?)(?:\.|,|!|$)"
    ), "location", "fact"),
    (re.compile(
        r"(?i)\bI\s+(?:am|'m)\s+from\s+(.+?)(?:\.|,|!|$)"
    ), "location", "fact"),

    # --- Employment ---
    (re.compile(
        r"(?i)\bI\s+(?:work|am working|am employed)\s+(?:at|for|with)\s+(.+?)(?:\.|,|!|$)"
    ), "employer", "fact"),
    (re.compile(
        r"(?i)\bI\s+(?:just\s+)?(?:joined|started|hired|got a job)\s+(?:at\s+)?(.+?)(?:\.|,|!|$)"
    ), "employer", "fact"),
    (re.compile(
        r"(?i)\bmy\s+(?:job|role|position|title)\s+(?:is|was)\s+(.+?)(?:\.|,|!|$)"
    ), "occupation", "fact"),

    # --- Pets ---
    (re.compile(
        r"(?i)\bI\s+(?:have|got|own)\s+a\s+(\w+)\s+(?:named|called)\s+(\w+)"
    ), "pet", "fact"),
    (re.compile(
        r"(?i)\bmy\s+(\w+)\s+(?:named|called)\s+(\w+)"
    ), "pet", "fact"),
    # Implicit pet detection
    (re.compile(
        r"(?i)\b(?:walking|feeding|playing\s+with)\s+(?:my\s+)?(\w+)\s+(?:named\s+)?(\w+)"
    ), "pet", "fact"),

    # --- Allergies ---
    (re.compile(
        r"(?i)\bI(?:'m| am)\s+(?:allergic|intolerant)\s+to\s+(.+?)(?:\.|,|!|$)"
    ), "allergy", "fact"),

    # --- Diet ---
    (re.compile(
        r"(?i)\bI(?:'m| am)\s+(vegetarian|vegan|pescatarian|keto|paleo|gluten[\s-]free)\b"
    ), "dietary_restriction", "fact"),
    (re.compile(
        r"(?i)\bI\s+(?:don't|do not)\s+eat\s+(.+?)(?:\.|,|!|$)"
    ), "dietary_restriction", "fact"),

    # --- Communication style ---
    (re.compile(
        r"(?i)\bI\s+(?:prefer|like|want)\s+(?:my\s+)?(?:answers?|responses?|replies?)\s+(?:to be\s+)?(.+?)(?:\.|,|!|$)"
    ), "communication_style", "preference"),
    (re.compile(
        r"(?i)\bplease\s+be\s+(concise|detailed|brief|short|direct|formal|casual)"
    ), "communication_style", "preference"),

    # --- Preferences (opinions) ---
    (re.compile(
        r"(?i)\bI\s+(?:love|hate|really\s+(?:like|dislike))\s+(.+?)(?:\.|,|!|$)"
    ), "preference", "preference"),

    # --- Name ---
    (re.compile(
        r"(?i)\bmy\s+name\s+(?:is|'s)\s+(.+?)(?:\.|,|!|$)"
    ), "name", "fact"),

    # --- Correction ---
    (re.compile(
        r"(?i)\b(?:actually|sorry|i meant)\s*[,:]?\s*(?:not\s+)?(.+?)(?:\.|,|!|$)"
    ), "correction", "fact"),
]
```

#### 2.3 Key normalization

```python
# Простая нормализация — без CANONICAL_MAP на 30 записей
KEY_ALIASES = {
    "company": "employer",
    "workplace": "employer",
    "city": "location",
    "hometown": "location",
    "job": "occupation",
    "role": "occupation",
    "position": "occupation",
    "title": "occupation",
    "diet": "dietary_restriction",
    "food_preference": "dietary_restriction",
}

def normalize_key(key: str) -> str:
    return KEY_ALIASES.get(key, key)
```

#### 2.4 Confidence by specificity

```python
# Чем специфичнее матч — тем выше confidence
def _confidence_for_match(key: str, value: str, source: str) -> float:
    base = 0.7
    if len(value) > 20:
        base += 0.05
    if key in ("employer", "location", "name", "allergy"):
        base += 0.1
    if source == "rule":
        base -= 0.05  # rules чуть менее уверены чем LLM
    return min(base, 0.85)
```

### Интеграция с extraction_service.py

Текущий flow (`extraction_service.py:41-44`):

```python
try:
    raw_memories = await llm_service.extract_memories(messages)
except Exception as e:
    raw_memories = RuleExtractor().extract(messages)
```

Новый flow — **rules + LLM параллельно, merge по приоритету LLM**:

```python
async def extract_and_store(self, messages, user_id, session_id, turn_id=None):
    if not user_id:
        return []

    # 1. Rules — всегда, мгновенно
    rule_memories = RuleExtractor().extract(messages)

    # 2. LLM — если есть ключ
    llm_memories = []
    if settings.llm_available:
        try:
            llm_memories = await llm_service.extract_memories(messages)
        except Exception as e:
            logger.warning(f"LLM extraction failed: {e}")

    # 3. Merge: LLM побеждает при конфликте ключа (богаче значения),
    #    rules заполняют gaps если LLM не вернул ключ
    raw_memories = self._merge_extractions(rule_memories, llm_memories)

    if not raw_memories:
        return []
    # ... далее как раньше
```

```python
def _merge_extractions(self, rules, llm):
    """LLM приоритетнее (богаче значения), rules заполняют gaps."""
    merged = {}

    for r in rules:
        key = normalize_key(r["key"])
        merged[key] = {**r, "key": key}

    for r in llm:
        key = normalize_key(r.get("key", ""))
        if key in merged:
            # LLM побеждает — значение богаче
            merged[key] = {**r, "key": key}
        else:
            merged[key] = {**r, "key": key}

    return list(merged.values())
```

### Критерий готовности

- Rule extractor извлекает из user-сообщений только
- ~15 категорий паттернов
- Key normalization работает ( employer = company = workplace )
- LLM extraction включён по умолчанию при наличии ключа
- Без ключа — rules-only, сервис не падает
- `_merge_extractions` корректно мержит

---

## Фаза 3: Разорвать DB txn перед LLM-вызовом

**Время:** ~1h
**Приоритет:** P1 — deadlock risk + eval timeout
**Проблема:** `SELECT FOR UPDATE` → network I/O → deadlock при concurrent writes

### Что менять

**Файлы:** `src/services/extraction_service.py`, `src/routers/turns.py`, `src/services/memory_service.py`

#### 3.1 Двухфазный commit в router

Текущий `src/routers/turns.py`:

```python
@router.post("/turns", status_code=201, response_model=TurnResponse)
async def create_turn(body: TurnCreate, db: AsyncSession = Depends(get_db)):
    service = MemoryService(db)
    ts = datetime.fromisoformat(body.timestamp.replace("Z", "+00:00"))
    turn = await service.store_turn(...)  # один вызов — одна транзакция
    await db.commit()
    return TurnResponse(id=str(turn.id))
```

Новый flow:

```python
@router.post("/turns", status_code=201, response_model=TurnResponse)
async def create_turn(body: TurnCreate, db: AsyncSession = Depends(get_db)):
    service = MemoryService(db)
    ts = datetime.fromisoformat(body.timestamp.replace("Z", "+00:00"))

    # Фаза 1: persist raw turn → commit (короткая транзакция)
    turn = await service.persist_turn(
        session_id=body.session_id,
        user_id=body.user_id,
        messages=[m.model_dump() for m in body.messages],
        timestamp=ts,
        metadata=body.metadata,
    )
    await db.commit()

    # Фаза 2: extraction + memories → commit (отдельная транзакция)
    if body.user_id:
        try:
            await service.extract_and_persist_memories(
                messages=[m.model_dump() for m in body.messages],
                user_id=body.user_id,
                session_id=body.session_id,
                turn_id=turn.id,
            )
            await db.commit()
        except Exception as e:
            logger.warning(f"Extraction failed for turn {turn.id}: {e}")
            # Rollback только memories, turn уже закоммичен
            await db.rollback()

    return TurnResponse(id=str(turn.id))
```

#### 3.2 MemoryService — split на два метода

```python
class MemoryService:
    # ...

    async def persist_turn(self, session_id, user_id, messages, timestamp, metadata=None):
        """Фаза 1: только persist turn. Без extraction."""
        return await self.turn_repo.create(session_id, user_id, messages, timestamp, metadata)

    async def extract_and_persist_memories(self, messages, user_id, session_id, turn_id=None):
        """Фаза 2: extraction + memory storage. После commit turn."""
        await self.extraction.extract_and_store(
            messages=messages,
            user_id=user_id,
            session_id=session_id,
            turn_id=turn_id,
        )
```

#### 3.3 Убрать `for_update` из `_resolve_memory`

В `extraction_service.py` заменить:

```python
# Было:
existing = await self.memory_repo.get_active_by_key(user_id, key, for_update=True)

# Стало:
existing = await self.memory_repo.get_active_by_key(user_id, key, for_update=False)
```

`for_update=True` был нужен для "read-your-write consistency", но теперь между persist turn и extraction — новый transaction scope. Для single-user челленджа это безопасно.

### Критерий готовности

- Turn persist → commit → LLM call → memory commit — два отдельных commit
- `for_update` убран из `get_active_by_key` в `_resolve_memory`
- При crash во время LLM-вызова turn остаётся в БД (memories потеряны — приемлемо)
- `/recall` после `/turns` возвращает данные (read-your-write через второй commit)

---

## Фаза 4: BM25-only fallback recall

**Время:** ~2h
**Приоритет:** P1 — без API-ключа recall бесполезен
**Проблема:** `_fallback_recall` возвращает последние 5 memories без relevance sorting

### Что менять

**Файл:** `src/services/recall_service.py`

#### 4.1 Новый метод `_bm25_fallback_recall`

```python
async def _bm25_fallback_recall(
    self, query: str, user_id: str, max_tokens: int
) -> tuple[str, list[dict]]:
    """Recall через BM25 когда embeddings недоступны."""
    bm25_results = await self.memory_repo.bm25_search(user_id, query, limit=20)

    if not bm25_results:
        # Совсем ничего — вернуть recent memories
        memories = await self.memory_repo.get_recent_by_user(user_id, limit=5)
        if not memories:
            return "", []
        return await self._fallback_recall(user_id, max_tokens)

    # Используем RRF merge с пустыми vector_results для единообразия
    fused = rrf_merge([], bm25_results)

    # Без LLM rerank — просто RRF + recency
    budget = max_tokens
    sections = []
    citations = []

    # Stable facts
    stable_facts = await self.memory_repo.get_stable_facts(user_id)
    facts_budget = int(budget * 0.35)
    facts_text = format_stable_facts(stable_facts, facts_budget)
    if facts_text:
        sections.append(facts_text)
    used = estimate_tokens(facts_text)

    # Query-relevant (BM25)
    relevant_budget = min(int(budget * 0.50), budget - used)
    relevant_text, relevant_citations = format_relevant_memories(fused, relevant_budget)
    if relevant_text:
        sections.append(relevant_text)
        citations.extend(relevant_citations)
    used += estimate_tokens(relevant_text)

    # Recent context
    remaining = budget - used - 50
    if remaining > 100:
        recent_turns = await self.turn_repo.get_recent_by_user(user_id, limit=3)
        recent_text = format_recent_turns(recent_turns, remaining)
        if recent_text:
            sections.append(recent_text)

    context = "\n\n".join(s for s in sections if s)
    return context, citations
```

#### 4.2 Интеграция в `recall()`

Заменить `_fallback_recall` на `_bm25_fallback_recall` в main recall path:

```python
async def recall(self, query, session_id, user_id=None, max_tokens=512):
    if not user_id:
        return await self._session_recall(query, session_id, max_tokens)

    # Query rewriting — только если LLM доступен
    sub_queries = [query]
    if settings.llm_available:
        sub_queries = await self._rewrite_query(query)

    # Embedding — только если LLM доступен
    if settings.llm_available:
        try:
            all_embeddings = await llm_service.embed(sub_queries)
        except Exception as e:
            logger.warning(f"Embedding failed, falling back to BM25: {e}")
            return await self._bm25_fallback_recall(query, user_id, max_tokens)
    else:
        # No API key — BM25-only recall
        return await self._bm25_fallback_recall(query, user_id, max_tokens)

    # ... rest of hybrid search as before
```

### Аналогично для SearchService

**Файл:** `src/services/search_service.py`

`_search_by_user` уже имеет `_fallback_search`, но он просто возвращает recent memories. Заменить:

```python
async def _fallback_search(self, query: str, user_id: str, limit: int) -> list[dict]:
    """BM25-only search когда embeddings недоступны."""
    bm25_results = await self.memory_repo.bm25_search(user_id, query, limit=limit)
    results = []
    for memory, score in bm25_results[:limit]:
        results.append({
            "content": f"{memory.key}: {memory.value}",
            "score": round(score, 4),
            "session_id": memory.source_session,
            "timestamp": memory.created_at.isoformat() if memory.created_at else "",
            "metadata": {"key": memory.key, "type": memory.type},
        })
    return results
```

### Критерий готовности

- Без OPENAI_API_KEY: `/recall` работает через BM25 + recency
- Без OPENAI_API_KEY: `/search` работает через BM25
- С ключом: hybrid pipeline работает как раньше
- Graceful fallback при embedding timeout

---

## Фаза 5: Тесты и fixtures

**Время:** ~3h
**Приоритет:** P1 — eval обязательно проверяет test coverage
**Проблема:** ~33 теста, часть требует Docker, нет hermetic conftest

### 5.1 Hermetic conftest

**Файл:** `tests/conftest.py` (создать или обновить)

```python
import os

# Force LLM off BEFORE any app imports
os.environ["LLM_ENABLED"] = "false"
os.environ.pop("OPENAI_API_KEY", None)
os.environ["DATABASE_URL"] = "postgresql+asyncpg://postgres:postgres@db:5432/memory"

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from src.main import app

@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
```

**Примечание:** Hermetic тесты для extraction/recall логики запускаются без БД (unit tests). Contract/integration тесты требуют Docker.

### 5.2 Contract tests — все 7 endpoints

**Файл:** `tests/contract/test_endpoints.py` — расширить

| Endpoint | Status | Тест |
|---|---|---|
| `GET /health` | 200 | Возвращает `{"status": "ok"}` или аналогичное |
| `POST /turns` | 201 | Возвращает `{"id": "..."}` |
| `POST /turns` | 422 | Неизвестное поле, missing fields, bad JSON |
| `POST /recall` | 200 | Возвращает `{"context": "...", "citations": [...]}` |
| `POST /recall` | 200 | Пустой ответ для холодной сессии |
| `POST /search` | 200 | Возвращает `{"results": [...]}` |
| `GET /users/{id}/memories` | 200 | Возвращает structured memories |
| `DELETE /sessions/{id}` | 204 | Нет контента |
| `DELETE /users/{id}` | 204 | Нет контента |

### 5.3 Robustness tests — malformed input

**Файл:** `tests/robustness/test_malformed_input.py` — расширить

Сценарии:
- Bad JSON → 422
- Missing required fields → 422
- Empty messages array → 422
- Unicode в content (эмодзи, RTL, CJK) → 201
- Oversized payload (100KB content) → 201
- SQL injection в query → 200 (не crash)
- Null values → 422
- Very long session_id → 201

### 5.4 Recall quality fixture

**Файлы:**
- `fixtures/conversations.json` — обновить (5 scripted conversations, 2 users, 5 sessions)
- `fixtures/probes.json` — создать

```json
{
  "probes": [
    {
      "id": "P1",
      "query": "Where does this user live?",
      "user_id": "user-1",
      "must_match": ["Berlin"],
      "must_not": ["NYC"],
      "max_tokens": 512
    },
    {
      "id": "P2",
      "query": "What is the user's dog's name?",
      "user_id": "user-1",
      "must_match": ["Biscuit"],
      "must_not": [],
      "max_tokens": 256
    },
    {
      "id": "P3",
      "query": "What city does the person whose dog is Biscuit live in?",
      "user_id": "user-1",
      "must_match": ["Berlin"],
      "must_not": [],
      "max_tokens": 512,
      "multi_hop": true
    },
    {
      "id": "P4",
      "query": "What's the capital of France?",
      "user_id": "user-1",
      "must_match": [],
      "must_not": [],
      "max_tokens": 256,
      "expect_empty": true
    }
  ]
}
```

**Файл:** `tests/recall_quality/test_recall_fixture.py` — обновить

```python
import json
import pytest

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"

@pytest.fixture
def conversations():
    with open(FIXTURES_DIR / "conversations.json") as f:
        return json.load(f)

@pytest.fixture
def probes():
    with open(FIXTURES_DIR / "probes.json") as f:
        return json.load(f)["probes"]

@pytest.mark.asyncio
async def test_recall_fixture_quality(client, conversations, probes):
    # 1. Ingest all conversations
    for conv in conversations:
        await client.post("/turns", json=conv)

    # 2. Run probes
    results = {"pass": 0, "fail": 0, "total": len(probes)}
    for probe in probes:
        resp = await client.post("/recall", json={
            "query": probe["query"],
            "session_id": f"probe-{probe['id']}",
            "user_id": probe["user_id"],
            "max_tokens": probe.get("max_tokens", 512),
        })
        assert resp.status_code == 200
        data = resp.json()
        context = data["context"].lower()

        if probe.get("expect_empty"):
            if not context:
                results["pass"] += 1
            else:
                results["fail"] += 1
            continue

        must_match = [m.lower() for m in probe["must_match"]]
        must_not = [m.lower() for m in probe["must_not"]]

        matched = all(m in context for m in must_match)
        avoided = all(m not in context for m in must_not)

        if matched and avoided:
            results["pass"] += 1
        else:
            results["fail"] += 1

    # Report
    print(f"\nRecall quality: {results['pass']}/{results['total']} passed")
    assert results["pass"] >= results["total"] * 0.6, \
        f"Too few probes passed: {results['pass']}/{results['total']}"
```

### 5.5 Concurrent sessions test

**Файл:** `tests/robustness/test_concurrent_sessions.py` — расширить

```python
@pytest.mark.asyncio
async def test_no_cross_user_bleed(client):
    # User A: Berlin
    await client.post("/turns", json={
        "session_id": "s-a",
        "user_id": "user-a",
        "messages": [{"role": "user", "content": "I live in Berlin"}],
        "timestamp": "2025-01-01T00:00:00Z",
        "metadata": {},
    })

    # User B: Tokyo
    await client.post("/turns", json={
        "session_id": "s-b",
        "user_id": "user-b",
        "messages": [{"role": "user", "content": "I live in Tokyo"}],
        "timestamp": "2025-01-01T00:00:00Z",
        "metadata": {},
    })

    # Recall for user A — must NOT mention Tokyo
    resp = await client.post("/recall", json={
        "query": "Where does this user live?",
        "session_id": "s-a-recall",
        "user_id": "user-a",
        "max_tokens": 512,
    })
    context = resp.json()["context"].lower()
    assert "berlin" in context
    assert "tokyo" not in context

    # Recall for user B — must NOT mention Berlin
    resp = await client.post("/recall", json={
        "query": "Where does this user live?",
        "session_id": "s-b-recall",
        "user_id": "user-b",
        "max_tokens": 512,
    })
    context = resp.json()["context"].lower()
    assert "tokyo" in context
    assert "berlin" not in context
```

### Критерий готовности

- 60+ тестов (contract + robustness + recall quality + concurrent)
- Hermetic unit tests не требуют Docker
- Fixture probes покрывают: single-hop, multi-hop, noise, cross-session
- Deterministic grading: must_match / must_not

---

## Порядок выполнения

```
Фаза 1 (30 мин) → Фаза 3 (1h) → Фаза 2 (3h) → Фаза 4 (2h) → Фаза 5 (3h)
     │                 │                │               │              │
     ▼                 ▼                ▼               ▼              ▼
extra=forbid    разорвать txn    усиление rules    BM25 fallback    тесты
4 файла         extraction       rule_extractor    recall_service   fixtures
                                  + merge          + search_svc
```

**Обоснование порядка:**
1. `extra="forbid"` — самое быстрое, сразу даёт contract compliance
2. Разрыв txn — фиксит deadlock risk перед тем как добавлять extraction logic
3. Усиление rules — главная фича, но зависит от txn fix
4. BM25 fallback — зависит от rules (rules без LLM должны работать)
5. Тесты — валидируют всё выше

---

## Что НЕ делаем (nice-to-have)

| Что | Почему не сейчас |
|---|---|
| Canonical key map (30 записей) | LLM prompt уже задаёт controlled vocabulary; key normalization в rules достаточна |
| Density gate | Текущий noise resistance уже работает (adaptive threshold) |
| Cross-key contradiction threshold 0.80 → 0.70 | Уже работает на 0.70, разница marginal |
| Opinion evolution arcs | Сложно за оставшееся время; текущий "latest stance per topic" приемлем |
| 120+ тестов | 60+ достаточно; 120 — overengineering для челленджа |
| `LLM_ENABLED` flag | Не нужен — достаточно `settings.llm_available` |
| Query rewrite skip для short queries | Minor optimization, не влияет на eval |

---

## Итого

| Фаза | Время | Файлы | Критерий |
|---|---|---|---|
| 1. extra=forbid | 30 мин | 4 schemas | 422 на unknown fields |
| 2. Rules + merge | 3h | rule_extractor, extraction_service | 15 категорий, subject gate, key norm |
| 3. Txn split | 1h | memory_service, turns router | Два commit, нет deadlock |
| 4. BM25 fallback | 2h | recall_service, search_service | Recall работает без API ключа |
| 5. Тесты | 3h | conftest, contract, robustness, fixture | 60+ тестов, fixture quality ≥ 60% |
| **Итого** | **~10h** | | |
