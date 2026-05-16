import uuid

import sqlalchemy
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.memory import Memory


class MemoryRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, user_id, type, key, value, confidence, source_session,
                     source_turn_id=None, supersedes=None) -> Memory:
        memory = Memory(
            user_id=user_id,
            type=type,
            key=key,
            value=value,
            confidence=confidence,
            source_session=source_session,
            source_turn_id=source_turn_id,
            supersedes=supersedes,
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

    async def get_active_by_key(self, user_id: str, key: str) -> list[Memory]:
        result = await self.session.execute(
            select(Memory)
            .where(Memory.user_id == user_id, Memory.key == key, Memory.active == True)
            .order_by(Memory.created_at.desc())
        )
        return list(result.scalars().all())

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
        stmt = text("""
            SELECT id, user_id, type, key, value, confidence, source_session,
                   source_turn_id, supersedes, active, created_at, updated_at,
                   1 - (embedding <=> :embedding) AS similarity
            FROM memories
            WHERE user_id = :user_id
              AND active = TRUE
              AND embedding IS NOT NULL
            ORDER BY embedding <=> :embedding
            LIMIT :limit
        """)
        result = await self.session.execute(
            stmt,
            {"user_id": user_id, "embedding": str(query_embedding), "limit": limit},
        )
        rows = result.fetchall()
        memories = []
        for row in rows:
            m = Memory(
                id=row.id, user_id=row.user_id, type=row.type, key=row.key,
                value=row.value, confidence=row.confidence,
                source_session=row.source_session, source_turn_id=row.source_turn_id,
                supersedes=row.supersedes, active=row.active,
                created_at=row.created_at, updated_at=row.updated_at,
            )
            memories.append((m, float(row.similarity)))
        return memories

    async def bm25_search(self, user_id: str, query: str,
                          limit: int = 20) -> list[tuple[Memory, float]]:
        stmt = text("""
            SELECT id, user_id, type, key, value, confidence, source_session,
                   source_turn_id, supersedes, active, created_at, updated_at,
                   ts_rank_cd(
                       search_vector,
                       plainto_tsquery('english', :query)
                   ) AS bm25_score
            FROM memories
            WHERE user_id = :user_id
              AND active = TRUE
              AND search_vector @@ plainto_tsquery('english', :query)
            ORDER BY bm25_score DESC
            LIMIT :limit
        """)
        result = await self.session.execute(
            stmt, {"user_id": user_id, "query": query, "limit": limit},
        )
        rows = result.fetchall()
        memories = []
        for row in rows:
            m = Memory(
                id=row.id, user_id=row.user_id, type=row.type, key=row.key,
                value=row.value, confidence=row.confidence,
                source_session=row.source_session, source_turn_id=row.source_turn_id,
                supersedes=row.supersedes, active=row.active,
                created_at=row.created_at, updated_at=row.updated_at,
            )
            memories.append((m, float(row.bm25_score)))
        return memories

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
        min_confidence: float = 0.8, min_similarity: float = 0.3,
    ) -> list[Memory]:
        stmt = text("""
            SELECT id, user_id, type, key, value, confidence, source_session,
                   source_turn_id, supersedes, active, created_at, updated_at
            FROM memories
            WHERE user_id = :user_id
              AND active = TRUE
              AND confidence >= :min_confidence
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> :embedding) >= :min_similarity
            ORDER BY (1 - (embedding <=> :embedding)) DESC
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
        rows = result.fetchall()
        memories = []
        for row in rows:
            m = Memory(
                id=row.id, user_id=row.user_id, type=row.type, key=row.key,
                value=row.value, confidence=row.confidence,
                source_session=row.source_session, source_turn_id=row.source_turn_id,
                supersedes=row.supersedes, active=row.active,
                created_at=row.created_at, updated_at=row.updated_at,
            )
            memories.append(m)
        return memories
