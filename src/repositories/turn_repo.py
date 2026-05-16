from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.turn import Turn


class TurnRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, session_id, user_id, messages, timestamp, metadata=None) -> Turn:
        turn = Turn(
            session_id=session_id,
            user_id=user_id,
            messages=messages,
            timestamp=timestamp,
            metadata_=metadata or {},
        )
        self.session.add(turn)
        await self.session.flush()
        await self.session.refresh(turn)
        return turn

    async def get_by_session(self, session_id: str, limit: int = 10) -> list[Turn]:
        result = await self.session.execute(
            select(Turn)
            .where(Turn.session_id == session_id)
            .order_by(Turn.timestamp.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_recent_by_user(self, user_id: str, limit: int = 3) -> list[Turn]:
        result = await self.session.execute(
            select(Turn)
            .where(Turn.user_id == user_id)
            .order_by(Turn.timestamp.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def delete_by_session(self, session_id: str) -> int:
        result = await self.session.execute(
            delete(Turn).where(Turn.session_id == session_id)
        )
        await self.session.flush()
        return result.rowcount

    async def delete_by_user(self, user_id: str) -> int:
        result = await self.session.execute(
            delete(Turn).where(Turn.user_id == user_id)
        )
        await self.session.flush()
        return result.rowcount
