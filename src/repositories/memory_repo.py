from __future__ import annotations

import uuid

import sqlalchemy
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.memory import Memory

_MEMORY_COLUMNS = (
    "id", "user_id", "type", "key", "value", "confidence", "source_session",
    "source_turn_id", "supersedes", "active", "created_at", "updated_at",
    "extraction_method", "turn_index", "provenance",
)


def _row_to_memory(row) -> Memory:
    return Memory(
        id=row.id, user_id=row.user_id, type=row.type, key=row.key,
        value=row.value, confidence=row.confidence,
        source_session=row.source_session, source_turn_id=row.source_turn_id,
        supersedes=row.supersedes, active=row.active,
        created_at=row.created_at, updated_at=row.updated_at,
        embedding=None, extraction_method=row.extraction_method,
        turn_index=row.turn_index, provenance=row.provenance,
    )


class MemoryRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, user_id, type, key, value, confidence, source_session,
                     source_turn_id=None, supersedes=None, extraction_method=None,
                     turn_index=None, provenance=None) -> Memory:
        memory = Memory(
            user_id=user_id,
            type=type,
            key=key,
            value=value,
            confidence=confidence,
            source_session=source_session,
            source_turn_id=source_turn_id,
            supersedes=supersedes,
            extraction_method=extraction_method,
            turn_index=turn_index,
            provenance=provenance,
        )
        self.session.add(memory)
        await self.session.flush()
        await self.session.refresh(memory)
        return memory

    async def get_active_by_user(self, user_id: str) -> list[Memory]:
        result = await self.session.execute(
            select(Memory)
            .where(Memory.user_id == user_id, Memory.active == True)
            .order_by(Memory.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_all_by_user(self, user_id: str) -> list[Memory]:
        result = await self.session.execute(
            select(Memory)
            .where(Memory.user_id == user_id)
            .order_by(Memory.key, Memory.active.desc(), Memory.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_by_session(self, session_id: str) -> list[Memory]:
        result = await self.session.execute(
            select(Memory).where(Memory.source_session == session_id)
        )
        return list(result.scalars().all())

    async def deactivate_by_id(self, memory_id: uuid.UUID) -> bool:
        result = await self.session.execute(
            update(Memory)
            .where(Memory.id == memory_id, Memory.active == True)
            .values(active=False, updated_at=sqlalchemy.func.now())
        )
        await self.session.flush()
        return result.rowcount > 0

    async def deactivate_by_key(self, user_id: str, key: str) -> int:
        result = await self.session.execute(
            update(Memory)
            .where(Memory.user_id == user_id, Memory.key == key, Memory.active == True)
            .values(active=False, updated_at=sqlalchemy.func.now())
        )
        await self.session.flush()
        return result.rowcount

    async def delete_by_user(self, user_id: str) -> int:
        result = await self.session.execute(
            delete(Memory).where(Memory.user_id == user_id)
        )
        await self.session.flush()
        return result.rowcount

    async def delete_by_session(self, session_id: str) -> int:
        result = await self.session.execute(
            delete(Memory).where(Memory.source_session == session_id)
        )
        await self.session.flush()
        return result.rowcount

    async def get_active_by_key(self, user_id: str, key: str, for_update: bool = False) -> list[Memory]:
        stmt = (
            select(Memory)
            .where(Memory.user_id == user_id, Memory.key == key, Memory.active == True)
            .order_by(Memory.created_at.desc())
        )
        if for_update:
            stmt = stmt.with_for_update()
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def key_search(self, user_id: str, keys: list[str],
                         limit: int = 20) -> list[tuple[Memory, float]]:
        if not keys:
            return []
        result = await self.session.execute(
            select(Memory)
            .where(
                Memory.user_id == user_id,
                Memory.active == True,
                Memory.key.in_(keys),
            )
            .order_by(Memory.created_at.desc())
            .limit(limit)
        )
        memories = list(result.scalars().all())
        return [(m, m.confidence) for m in memories]

    async def get_recent_by_user(self, user_id: str, limit: int = 10) -> list[Memory]:
        result = await self.session.execute(
            select(Memory)
            .where(Memory.user_id == user_id, Memory.active == True)
            .order_by(Memory.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_superseded_chain(self, memory_id: uuid.UUID) -> list[Memory]:
        result = await self.session.execute(
            select(Memory)
            .where(Memory.id == memory_id)
        )
        current = result.scalar_one_or_none()
        if not current or not current.supersedes:
            return []

        chain = []
        next_id = current.supersedes
        while next_id:
            result = await self.session.execute(
                select(Memory).where(Memory.id == next_id)
            )
            older = result.scalar_one_or_none()
            if not older:
                break
            chain.append(older)
            next_id = older.supersedes
        return chain

    async def vector_search(self, user_id: str, query_embedding: list[float],
                            limit: int = 20) -> list[tuple[Memory, float]]:
        cols = ", ".join(f"m.{c}" for c in _MEMORY_COLUMNS)
        stmt = text(f"""
            SELECT {cols},
                   1 - (embedding <=> :embedding) AS similarity
            FROM memories m
            WHERE m.user_id = :user_id
              AND m.active = TRUE
              AND m.embedding IS NOT NULL
            ORDER BY m.embedding <=> :embedding
            LIMIT :limit
        """)
        result = await self.session.execute(
            stmt,
            {"user_id": user_id, "embedding": str(query_embedding), "limit": limit},
        )
        return [(_row_to_memory(row), float(row.similarity)) for row in result.fetchall()]

    async def bm25_search(self, user_id: str, query: str,
                          limit: int = 20) -> list[tuple[Memory, float]]:
        cols = ", ".join(f"m.{c}" for c in _MEMORY_COLUMNS)
        stmt = text(f"""
            WITH tsq AS (
                SELECT COALESCE(
                    websearch_to_tsquery('english', :query),
                    plainto_tsquery('english', :query)
                ) AS q
            )
            SELECT {cols},
                   ts_rank_cd(m.search_vector, tsq.q) AS bm25_score
            FROM memories m, tsq
            WHERE m.user_id = :user_id
              AND m.active = TRUE
              AND m.search_vector @@ tsq.q
            ORDER BY bm25_score DESC
            LIMIT :limit
        """)
        result = await self.session.execute(
            stmt, {"user_id": user_id, "query": query, "limit": limit},
        )
        return [(_row_to_memory(row), float(row.bm25_score)) for row in result.fetchall()]

    async def find_cross_key_similar(
        self, user_id: str, embedding: list[float], exclude_key: str,
        exclude_id: uuid.UUID | None = None,
        threshold: float = 0.8, limit: int = 5,
    ) -> list[tuple[Memory, float]]:
        conditions = [
            "m.user_id = :user_id",
            "m.active = TRUE",
            "m.embedding IS NOT NULL",
            "m.key != :exclude_key",
            "1 - (m.embedding <=> :embedding) >= :threshold",
        ]
        params = {
            "user_id": user_id,
            "embedding": str(embedding),
            "exclude_key": exclude_key,
            "threshold": threshold,
            "limit": limit,
        }
        if exclude_id:
            conditions.append("m.id != :exclude_id")
            params["exclude_id"] = str(exclude_id)

        cols = ", ".join(f"m.{c}" for c in _MEMORY_COLUMNS)
        stmt = text(f"""
            SELECT {cols},
                   1 - (m.embedding <=> :embedding) AS similarity
            FROM memories m
            WHERE {' AND '.join(conditions)}
            ORDER BY m.embedding <=> :embedding
            LIMIT :limit
        """)
        result = await self.session.execute(stmt, params)
        return [(_row_to_memory(row), float(row.similarity)) for row in result.fetchall()]

    async def get_stable_facts(self, user_id: str, min_confidence: float = 0.8) -> list[Memory]:
        result = await self.session.execute(
            select(Memory)
            .where(
                Memory.user_id == user_id,
                Memory.active == True,
                Memory.confidence >= min_confidence,
            )
            .order_by(Memory.confidence.desc(), Memory.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_relevant_facts(
        self, user_id: str, query_embedding: list[float],
        min_confidence: float = 0.8, min_similarity: float = 0.35,
    ) -> list[Memory]:
        stmt = text(f"""
            SELECT m.{', m.'.join(_MEMORY_COLUMNS)}
            FROM memories m
            WHERE m.user_id = :user_id
              AND m.active = TRUE
              AND m.confidence >= :min_confidence
              AND m.embedding IS NOT NULL
              AND 1 - (m.embedding <=> :embedding) >= :min_similarity
            ORDER BY (1 - (m.embedding <=> :embedding)) DESC
        """)
        result = await self.session.execute(
            stmt,
            {
                "user_id": user_id,
                "embedding": str(query_embedding),
                "min_confidence": min_confidence,
                "min_similarity": min_similarity,
            },
        )
        return [_row_to_memory(row) for row in result.fetchall()]
