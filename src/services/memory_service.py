from sqlalchemy.ext.asyncio import AsyncSession

from src.repositories.memory_repo import MemoryRepo
from src.repositories.turn_repo import TurnRepo


class MemoryService:
    def __init__(self, session: AsyncSession):
        self.turn_repo = TurnRepo(session)
        self.memory_repo = MemoryRepo(session)
        self.session = session

    async def store_turn(self, session_id, user_id, messages, timestamp, metadata=None):
        return await self.turn_repo.create(session_id, user_id, messages, timestamp, metadata)

    async def get_user_memories(self, user_id: str):
        return await self.memory_repo.get_active_by_user(user_id)

    async def delete_session(self, session_id: str):
        await self.memory_repo.delete_by_session(session_id)
        await self.turn_repo.delete_by_session(session_id)
        await self.session.commit()

    async def delete_user(self, user_id: str):
        await self.memory_repo.delete_by_user(user_id)
        await self.turn_repo.delete_by_user(user_id)
        await self.session.commit()
