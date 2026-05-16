import asyncio
import logging
import uuid
from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession

from src.repositories.memory_repo import MemoryRepo
from src.repositories.turn_repo import TurnRepo
from src.services.llm_service import llm_service

logger = logging.getLogger(__name__)

RRF_K = 60
RERANK_TOP_K = 15
RECALL_RELEVANCE_THRESHOLD = 0.25


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def rrf_merge(
    vector_results: list[tuple], bm25_results: list[tuple], k: int = RRF_K
) -> list[tuple]:
    scores: dict[uuid.UUID, float] = {}
    memories: dict[uuid.UUID, object] = {}

    for rank, (memory, _score) in enumerate(vector_results):
        scores[memory.id] = scores.get(memory.id, 0) + 1.0 / (k + rank + 1)
        memories[memory.id] = memory

    for rank, (memory, _score) in enumerate(bm25_results):
        scores[memory.id] = scores.get(memory.id, 0) + 1.0 / (k + rank + 1)
        memories[memory.id] = memory

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [(memories[mid], score) for mid, score in ranked]


def _group_by_key(memories: list) -> dict[str, list]:
    grouped = defaultdict(list)
    for m in memories:
        grouped[m.key].append(m)
    for key in grouped:
        grouped[key].sort(key=lambda m: m.created_at, reverse=True)
    return dict(grouped)


def format_stable_facts(memories: list, budget_tokens: int) -> str:
    if not memories:
        return ""

    grouped = _group_by_key(memories)
    lines = []
    used = 0
    budget_chars = budget_tokens * 4

    for key, mems in grouped.items():
        if len(mems) == 1:
            m = mems[0]
            line = f"- **{m.key}**: {m.value}"
        else:
            newest = mems[0]
            older = mems[1:]
            evolution = "; ".join(o.value for o in reversed(older))
            line = f"- **{newest.key}**: {newest.value} (evolved from: {evolution})"

        if used + len(line) + 1 > budget_chars:
            break
        lines.append(line)
        used += len(line) + 1

    if not lines:
        return ""
    return "## User Profile\n" + "\n".join(lines)


def format_relevant_memories(
    memories: list[tuple], budget_tokens: int
) -> tuple[str, list[dict]]:
    if not memories:
        return "", []

    lines = []
    citations = []
    used = 0
    budget_chars = budget_tokens * 4
    seen_keys: dict[str, list] = defaultdict(list)

    for memory, score in memories:
        seen_keys[memory.key].append((memory, score))

    for key, items in seen_keys.items():
        if len(items) == 1:
            memory, score = items[0]
            line = f"- [{memory.type}/{memory.key}] {memory.value}"
            if used + len(line) + 1 > budget_chars:
                break
            lines.append(line)
            used += len(line) + 1
            citations.append({
                "turn_id": str(memory.source_turn_id) if memory.source_turn_id else None,
                "score": round(score, 4),
                "snippet": memory.value[:100],
            })
        else:
            newest_mem, newest_score = items[0]
            older_values = [m.value for m, _ in reversed(items[1:])]
            evolution = " → ".join(older_values + [newest_mem.value])
            line = f"- [{newest_mem.type}/{key}] {evolution}"
            if used + len(line) + 1 > budget_chars:
                break
            lines.append(line)
            used += len(line) + 1
            for memory, score in items:
                citations.append({
                    "turn_id": str(memory.source_turn_id) if memory.source_turn_id else None,
                    "score": round(score, 4),
                    "snippet": memory.value[:100],
                })

    if not lines:
        return "", []
    return "## Query-Relevant Context\n" + "\n".join(lines), citations


def format_recent_turns(turns: list, budget_tokens: int) -> str:
    lines = []
    used = 0
    budget_chars = budget_tokens * 4

    for turn in reversed(turns):
        for msg in turn.messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            line = f"{role}: {content}"
            if used + len(line) + 1 > budget_chars:
                break
            lines.append(line)
            used += len(line) + 1

    if not lines:
        return ""
    return "## Recent Conversation\n" + "\n".join(lines)


class RecallService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.memory_repo = MemoryRepo(session)
        self.turn_repo = TurnRepo(session)

    async def recall(
        self,
        query: str,
        session_id: str,
        user_id: str | None = None,
        max_tokens: int = 512,
    ) -> tuple[str, list[dict]]:
        if not user_id:
            return await self._session_recall(query, session_id, max_tokens)

        try:
            query_embeddings = await llm_service.embed([query])
            query_embedding = query_embeddings[0]
        except Exception as e:
            logger.warning(f"Query embedding failed, falling back to recent: {e}")
            return await self._fallback_recall(user_id, max_tokens)

        vector_results, bm25_results = await self._hybrid_search(user_id, query, query_embedding)

        fused = rrf_merge(vector_results, bm25_results)
        if not fused:
            return await self._fallback_recall(user_id, max_tokens)

        reranked = await self._rerank(query, fused)

        return await self._assemble_context(
            query, user_id, reranked, session_id, max_tokens, query_embedding
        )

    async def _hybrid_search(
        self, user_id: str, query: str, query_embedding: list[float]
    ) -> tuple[list, list]:
        vector_coro = self.memory_repo.vector_search(user_id, query_embedding, limit=20)
        bm25_coro = self.memory_repo.bm25_search(user_id, query, limit=20)

        vector_results, bm25_results = await asyncio.gather(
            vector_coro, bm25_coro, return_exceptions=True
        )

        if isinstance(vector_results, Exception):
            logger.warning(f"Vector search failed: {vector_results}")
            vector_results = []
        if isinstance(bm25_results, Exception):
            logger.warning(f"BM25 search failed: {bm25_results}")
            bm25_results = []

        return vector_results, bm25_results

    async def _rerank(self, query: str, fused: list[tuple]) -> list[tuple]:
        top_k = fused[:RERANK_TOP_K]
        if len(top_k) <= 1:
            return top_k

        memories_for_rerank = [
            {"value": m.value, "type": m.type, "key": m.key}
            for m, _score in top_k
        ]

        try:
            ranked_indices = await llm_service.rerank(query, memories_for_rerank)
        except Exception as e:
            logger.warning(f"LLM rerank failed, using RRF order: {e}")
            return top_k

        reranked = []
        for idx in ranked_indices:
            if 0 <= idx < len(top_k):
                reranked.append(top_k[idx])

        seen = set(ranked_indices)
        for i, item in enumerate(top_k):
            if i not in seen:
                reranked.append(item)

        return reranked

    async def _assemble_context(
        self,
        query: str,
        user_id: str,
        reranked: list[tuple],
        session_id: str | None,
        max_tokens: int,
        query_embedding: list[float] | None = None,
    ) -> tuple[str, list[dict]]:
        budget = max_tokens
        sections = []
        citations = []

        # Phase 1: Stable facts (35% budget) — only if query-relevant
        facts_budget = int(budget * 0.35)
        if not reranked:
            stable_facts = []
        elif query_embedding:
            stable_facts = await self.memory_repo.get_relevant_facts(
                user_id, query_embedding, min_similarity=RECALL_RELEVANCE_THRESHOLD
            )
        else:
            stable_facts = await self.memory_repo.get_stable_facts(user_id)

        facts_text = format_stable_facts(stable_facts, facts_budget)
        if facts_text:
            sections.append(facts_text)
        used = estimate_tokens(facts_text)

        # Phase 2: Query-relevant memories (50% budget)
        relevant_budget = int(budget * 0.50)
        relevant_text, relevant_citations = format_relevant_memories(reranked, relevant_budget)
        if relevant_text:
            sections.append(relevant_text)
            citations.extend(relevant_citations)
        used += estimate_tokens(relevant_text)

        # Phase 3: Recent session context (remaining budget)
        remaining = budget - used - 50
        if remaining > 100 and session_id:
            recent_turns = await self.turn_repo.get_by_session(session_id, limit=3)
            recent_text = format_recent_turns(recent_turns, remaining)
            if recent_text:
                sections.append(recent_text)

        context = "\n\n".join(s for s in sections if s)
        return context, citations

    async def _fallback_recall(
        self, user_id: str, max_tokens: int
    ) -> tuple[str, list[dict]]:
        memories = await self.memory_repo.get_recent_by_user(user_id, limit=5)
        if not memories:
            return "", []

        lines = []
        citations = []
        for m in memories:
            line = f"- [{m.type}/{m.key}] {m.value}"
            lines.append(line)
            citations.append({
                "turn_id": str(m.source_turn_id) if m.source_turn_id else None,
                "score": 1.0,
                "snippet": m.value[:100],
            })

        context = "## Recent Memories\n" + "\n".join(lines)
        max_chars = max_tokens * 4
        if len(context) > max_chars:
            context = context[:max_chars]
        return context, citations

    async def _session_recall(
        self, query: str, session_id: str, max_tokens: int
    ) -> tuple[str, list[dict]]:
        memories = await self.memory_repo.get_by_session(session_id)
        turns = await self.turn_repo.get_by_session(session_id, limit=3)

        sections = []
        citations = []
        used = 0

        if memories:
            active = [m for m in memories if m.active]
            if active:
                lines = []
                budget_chars = max_tokens * 4
                for m in active:
                    line = f"- [{m.type}/{m.key}] {m.value}"
                    if used + len(line) + 1 > budget_chars:
                        break
                    lines.append(line)
                    used += len(line) + 1
                    citations.append({
                        "turn_id": str(m.source_turn_id) if m.source_turn_id else None,
                        "score": 1.0,
                        "snippet": m.value[:100],
                    })
                if lines:
                    sections.append("## Session Memories\n" + "\n".join(lines))

        remaining = max_tokens - estimate_tokens("\n\n".join(sections)) - 50
        if remaining > 100 and turns:
            recent_text = format_recent_turns(turns, remaining)
            if recent_text:
                sections.append(recent_text)

        context = "\n\n".join(s for s in sections if s)
        return context, citations
