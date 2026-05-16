from sqlalchemy import delete, select, update
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

    async def get_by_session(self, session_id: str) -> list[Memory]:
        result = await self.session.execute(
            select(Memory).where(Memory.source_session == session_id)
        )
        return list(result.scalars().all())

    async def deactivate_by_key(self, user_id: str, key: str) -> int:
        result = await self.session.execute(
            update(Memory)
            .where(Memory.user_id == user_id, Memory.key == key, Memory.active == True)
            .values(active=False, updated_at=__import__("sqlalchemy").func.now())
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

    async def get_recent_by_user(self, user_id: str, limit: int = 10) -> list[Memory]:
        result = await self.session.execute(
            select(Memory)
            .where(Memory.user_id == user_id, Memory.active == True)
            .order_by(Memory.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
