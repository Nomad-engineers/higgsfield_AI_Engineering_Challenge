from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.repositories.memory_repo import MemoryRepo
from src.services.llm_service import llm_service
from src.services.recall_service import rrf_merge
from src.services.query import analyze_query, expand_query_for_bm25

logger = logging.getLogger(__name__)


SEARCH_RERANK_CANDIDATES = 10


class SearchService:
    def __init__(self, session: AsyncSession):
        self.memory_repo = MemoryRepo(session)

    async def search(
        self, query: str, user_id: str | None = None, session_id: str | None = None, limit: int = 10
    ) -> list[dict]:
        if not user_id and not session_id:
            return []

        if user_id:
            return await self._search_by_user(query, user_id, limit)

        return await self._search_by_session(session_id, limit)

    async def _search_by_user(self, query: str, user_id: str, limit: int) -> list[dict]:
        if not settings.llm_available:
            return await self._fallback_search(query, user_id, limit)

        try:
            query_embeddings = await llm_service.embed([query])
            query_embedding = query_embeddings[0]
        except Exception as e:
            logger.warning(f"Query embedding failed for search, falling back to BM25: {e}")
            return await self._fallback_search(query, user_id, limit)

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

        fused = rrf_merge(vector_results, bm25_results)

        reranked = await self._rerank(query, fused)

        results = []
        for memory, score in reranked[:limit]:
            results.append({
                "content": f"{memory.key}: {memory.value}",
                "score": round(score, 4),
                "session_id": memory.source_session,
                "timestamp": memory.created_at.isoformat() if memory.created_at else "",
                "metadata": {"key": memory.key, "type": memory.type},
            })

        return results

    async def _rerank(self, query: str, fused: list[tuple]) -> list[tuple]:
        candidates = fused[:SEARCH_RERANK_CANDIDATES]
        if len(candidates) <= 1:
            return candidates

        memories_for_rerank = [
            {"value": m.value, "type": m.type, "key": m.key}
            for m, _score in candidates
        ]

        try:
            result = await llm_service.rerank(query, memories_for_rerank)
            ranked_indices = result["ranked_indices"]
        except Exception as e:
            logger.warning(f"Search rerank failed, using RRF order: {e}")
            return candidates

        reranked = []
        for idx in ranked_indices:
            if 0 <= idx < len(candidates):
                reranked.append(candidates[idx])

        seen = set(ranked_indices)
        for i, item in enumerate(candidates):
            if i not in seen:
                reranked.append(item)

        # Append any results beyond the reranked window
        reranked.extend(fused[SEARCH_RERANK_CANDIDATES:])

        return reranked

    async def _search_by_session(self, session_id: str, limit: int) -> list[dict]:
        memories = await self.memory_repo.get_by_session(session_id)
        active = [m for m in memories if m.active][:limit]
        results = []
        for m in active:
            results.append({
                "content": f"{m.key}: {m.value}",
                "score": m.confidence,
                "session_id": m.source_session,
                "timestamp": m.created_at.isoformat() if m.created_at else "",
                "metadata": {"key": m.key, "type": m.type},
            })
        return results

    async def _fallback_search(self, query: str, user_id: str, limit: int) -> list[dict]:
        expanded_query = expand_query_for_bm25(query)
        bm25_results = await self.memory_repo.bm25_search(user_id, expanded_query, limit=limit)

        hints = analyze_query(query)
        key_results = []
        if hints["hint_keys"]:
            key_results = await self.memory_repo.key_search(
                user_id, list(hints["hint_keys"]), limit=limit
            )

        if not bm25_results and not key_results:
            memories = await self.memory_repo.get_recent_by_user(user_id, limit=limit)
            results = []
            for m in memories:
                results.append({
                    "content": f"{m.key}: {m.value}",
                    "score": m.confidence,
                    "session_id": m.source_session,
                    "timestamp": m.created_at.isoformat() if m.created_at else "",
                    "metadata": {"key": m.key, "type": m.type},
                })
            return results

        fused = rrf_merge([], bm25_results, key_results)

        results = []
        for memory, score in fused[:limit]:
            results.append({
                "content": f"{memory.key}: {memory.value}",
                "score": round(score, 4),
                "session_id": memory.source_session,
                "timestamp": memory.created_at.isoformat() if memory.created_at else "",
                "metadata": {"key": memory.key, "type": memory.type},
            })
        return results
