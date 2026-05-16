import asyncio
import logging
from datetime import datetime, timezone
import uuid
from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.repositories.memory_repo import MemoryRepo
from src.repositories.turn_repo import TurnRepo
from src.services.llm_service import llm_service

logger = logging.getLogger(__name__)

RRF_K = 60
TEMPORAL_ALPHA = 0.1
RERANK_TOP_K = 15
RECALL_RELEVANCE_THRESHOLD = 0.35
RERANK_NOISE_FLOOR = 0.35
STABLE_FACTS_MIN_DENSITY = 0.30


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 3)


def rrf_merge(
    vector_results: list[tuple], bm25_results: list[tuple], k: int = RRF_K
) -> list[tuple]:
    scores: dict[uuid.UUID, float] = {}
    memories: dict[uuid.UUID, object] = {}
    now = datetime.now(timezone.utc)

    for rank, (memory, _score) in enumerate(vector_results):
        scores[memory.id] = scores.get(memory.id, 0) + 1.0 / (k + rank + 1)
        memories[memory.id] = memory

    for rank, (memory, _score) in enumerate(bm25_results):
        scores[memory.id] = scores.get(memory.id, 0) + 1.0 / (k + rank + 1)
        memories[memory.id] = memory

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    result = []
    for mid, score in ranked:
        mem = memories[mid]
        days_old = (now - mem.created_at).days if mem.created_at else 0
        recency = 1.0 / (1.0 + days_old)
        adjusted = score * (1 + TEMPORAL_ALPHA * recency)
        result.append((mem, adjusted))
    return result


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
    budget_chars = budget_tokens * 3
    compact = budget_tokens < 256

    for key, mems in grouped.items():
        if len(mems) == 1:
            m = mems[0]
            line = f"{m.key}: {m.value}" if compact else f"- **{m.key}**: {m.value}"
        else:
            newest = mems[0]
            older = mems[1:]
            evolution = "; ".join(o.value for o in reversed(older))
            if compact:
                line = f"{newest.key}: {newest.value} (from {evolution})"
            else:
                line = f"- **{newest.key}**: {newest.value} (evolved from: {evolution})"

        if used + len(line) + 1 > budget_chars:
            break
        lines.append(line)
        used += len(line) + 1

    if not lines:
        return ""
    if compact:
        return "\n".join(lines)
    return "## User Profile\n" + "\n".join(lines)


def format_relevant_memories(
    memories: list[tuple], budget_tokens: int
) -> tuple[str, list[dict]]:
    if not memories:
        return "", []

    lines = []
    citations = []
    used = 0
    budget_chars = budget_tokens * 3
    seen_keys: dict[str, list] = defaultdict(list)
    compact = budget_tokens < 256

    for memory, score in memories:
        seen_keys[memory.key].append((memory, score))

    for key, items in seen_keys.items():
        if len(items) == 1:
            memory, score = items[0]
            if compact:
                line = f"[{memory.type}/{memory.key}] {memory.value}"
            else:
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
            if compact:
                line = f"[{newest_mem.type}/{key}] {evolution}"
            else:
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
    if compact:
        return "\n".join(lines), citations
    return "## Query-Relevant Context\n" + "\n".join(lines), citations


def format_recent_turns(turns: list, budget_tokens: int) -> str:
    lines = []
    used = 0
    budget_chars = budget_tokens * 3

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

        # No LLM key — BM25-only recall
        if not settings.llm_available:
            return await self._bm25_fallback_recall(query, user_id, max_tokens)

        # Query rewriting: decompose multi-hop queries into sub-queries
        sub_queries = await self._rewrite_query(query)

        # Embed all sub-queries in one batch
        try:
            all_embeddings = await llm_service.embed(sub_queries)
        except Exception as e:
            logger.warning(f"Query embedding failed, falling back to BM25: {e}")
            return await self._bm25_fallback_recall(query, user_id, max_tokens)

        # Run hybrid search for each sub-query and merge results
        all_vector_results: list[tuple] = []
        all_bm25_results: list[tuple] = []
        similarity_map: dict[uuid.UUID, float] = {}

        for sq, emb in zip(sub_queries, all_embeddings):
            vec_res, bm25_res = await self._hybrid_search(user_id, sq, emb)
            all_vector_results.extend(vec_res)
            all_bm25_results.extend(bm25_res)
            for m, sim in vec_res:
                if m.id not in similarity_map or sim > similarity_map[m.id]:
                    similarity_map[m.id] = sim

        fused = rrf_merge(all_vector_results, all_bm25_results)
        if not fused:
            return await self._bm25_fallback_recall(query, user_id, max_tokens)

        reranked = await self._rerank(query, fused, sub_queries)

        query_embedding = all_embeddings[0]

        return await self._assemble_context(
            query, user_id, reranked, session_id, max_tokens,
            query_embedding, similarity_map,
        )

    async def _rewrite_query(self, query: str) -> list[str]:
        try:
            result = await llm_service.rewrite_query(query)
        except Exception as e:
            logger.warning(f"Query rewrite failed, using original: {e}")
            return [query]

        if not result.get("is_multi_hop", False):
            return [query]

        sub_queries = result.get("sub_queries", [])
        if not sub_queries or len(sub_queries) < 2:
            return [query]

        logger.info(
            f"Query rewritten: '{query[:60]}' -> {len(sub_queries)} sub-queries"
        )
        return sub_queries

    async def _hybrid_search(
        self, user_id: str, query: str, query_embedding: list[float]
    ) -> tuple[list, list]:
        vector_coro = self.memory_repo.vector_search(user_id, query_embedding, limit=30)
        bm25_coro = self.memory_repo.bm25_search(user_id, query, limit=30)

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

    async def _rerank(
        self, query: str, fused: list[tuple], sub_queries: list[str] | None = None
    ) -> list[tuple]:
        top_k = fused[:RERANK_TOP_K]
        if len(top_k) <= 1:
            return top_k

        memories_for_rerank = [
            {"value": m.value, "type": m.type, "key": m.key}
            for m, _score in top_k
        ]

        try:
            result = await llm_service.rerank(query, memories_for_rerank, sub_queries)
        except Exception as e:
            logger.warning(f"LLM rerank failed, using RRF order: {e}")
            return top_k

        ranked_indices = result["ranked_indices"]
        groups = result.get("groups", [])

        reranked = []
        for idx in ranked_indices:
            if 0 <= idx < len(top_k):
                reranked.append(top_k[idx])

        seen = set(ranked_indices)
        for i, item in enumerate(top_k):
            if i not in seen:
                reranked.append(item)

        if not groups:
            return reranked

        grouped_indices = set()
        for g in groups:
            for idx in g["indices"]:
                if 0 <= idx < len(top_k):
                    grouped_indices.add(idx)

        if not grouped_indices:
            return reranked

        # Ensure all group members are present, then append any missing at the front
        seen_ids = {id(top_k[i][0]) for i in seen if 0 <= i < len(top_k)}
        group_items = []
        for idx in sorted(grouped_indices):
            if id(top_k[idx][0]) not in {id(it[0]) for it in group_items}:
                group_items.append(top_k[idx])

        non_group_items = [it for it in reranked if id(it[0]) not in {id(g[0]) for g in group_items}]

        return group_items + non_group_items

    async def _assemble_context(
        self,
        query: str,
        user_id: str,
        reranked: list[tuple],
        session_id: str | None,
        max_tokens: int,
        query_embedding: list[float] | None = None,
        similarity_map: dict | None = None,
    ) -> tuple[str, list[dict]]:
        budget = max_tokens
        sections = []
        citations = []

        # Gate query-relevant results by vector similarity
        if similarity_map and reranked:
            sims = {m.id: similarity_map.get(m.id, 0) for m, _ in reranked}
            max_sim = max(sims.values())

            if max_sim < RECALL_RELEVANCE_THRESHOLD:
                reranked = []
            elif max_sim < 0.50:
                reranked = [
                    (m, score) for m, score in reranked
                    if sims[m.id] >= RERANK_NOISE_FLOOR
                ]
            else:
                threshold = max(RERANK_NOISE_FLOOR, max_sim * 0.5)
                reranked = [
                    (m, score) for m, score in reranked
                    if sims[m.id] >= threshold
                ]

        # Phase 1: Stable facts — with relevance density gating
        skip_stable = False
        if not reranked:
            stable_facts = []
            skip_stable = True
        elif query_embedding:
            stable_facts = await self.memory_repo.get_relevant_facts(
                user_id, query_embedding, min_similarity=RECALL_RELEVANCE_THRESHOLD
            )
            all_stable = await self.memory_repo.get_stable_facts(user_id)
            if all_stable and len(stable_facts) / len(all_stable) < STABLE_FACTS_MIN_DENSITY:
                stable_facts = []
                skip_stable = True
        else:
            stable_facts = await self.memory_repo.get_stable_facts(user_id)
            if not stable_facts:
                skip_stable = True

        if skip_stable:
            facts_budget = 0
            relevant_budget = int(budget * 0.60)
        else:
            facts_budget = int(budget * 0.35)
            relevant_budget = int(budget * 0.50)

        facts_text = format_stable_facts(stable_facts, facts_budget)
        if facts_text:
            sections.append(facts_text)
        used = estimate_tokens(facts_text)

        # Phase 2: Query-relevant memories
        relevant_budget = min(relevant_budget, budget - used)
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

        # Hard cap: ensure we don't exceed max_tokens by more than 20%
        max_chars = int(max_tokens * 3 * 1.2)
        if len(context) > max_chars:
            context = context[:max_chars] + "..."

        return context, citations

    async def _bm25_fallback_recall(
        self, query: str, user_id: str, max_tokens: int
    ) -> tuple[str, list[dict]]:
        """BM25-based recall when embeddings/LLM are unavailable."""
        bm25_results = await self.memory_repo.bm25_search(user_id, query, limit=20)

        if not bm25_results:
            return "", []

        fused = rrf_merge([], bm25_results)

        budget = max_tokens
        sections = []
        citations = []

        # Stable facts (no embedding filter needed)
        stable_facts = await self.memory_repo.get_stable_facts(user_id)
        facts_budget = int(budget * 0.35)
        facts_text = format_stable_facts(stable_facts, facts_budget)
        if facts_text:
            sections.append(facts_text)
        used = estimate_tokens(facts_text)

        # Query-relevant (BM25 ranked)
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

        max_chars = int(max_tokens * 3 * 1.2)
        if len(context) > max_chars:
            context = context[:max_chars] + "..."

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
        max_chars = max_tokens * 3
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
                budget_chars = max_tokens * 3
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
