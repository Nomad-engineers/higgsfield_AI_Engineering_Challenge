import logging

from sqlalchemy.ext.asyncio import AsyncSession

from src.repositories.memory_repo import MemoryRepo
from src.repositories.turn_repo import TurnRepo
from src.services.extraction_service import ExtractionService

logger = logging.getLogger(__name__)


class MemoryService:
    def __init__(self, session: AsyncSession):
        self.turn_repo = TurnRepo(session)
        self.memory_repo = MemoryRepo(session)
        self.session = session
        self.extraction = ExtractionService(session)

    async def store_turn(self, session_id, user_id, messages, timestamp, metadata=None):
        turn = await self.turn_repo.create(session_id, user_id, messages, timestamp, metadata)

        if user_id:
            try:
                await self.extraction.extract_and_store(
                    messages=messages,
                    user_id=user_id,
                    session_id=session_id,
                    turn_id=turn.id,
                )
            except Exception as e:
                logger.warning(f"Extraction pipeline failed for turn {turn.id}: {e}")

        return turn

    async def get_user_memories(self, user_id: str):
        return await self.memory_repo.get_active_by_user(user_id)

    async def get_user_memories_with_history(self, user_id: str):
        return await self.memory_repo.get_all_by_user(user_id)

    async def delete_session(self, session_id: str):
        await self.memory_repo.delete_by_session(session_id)
        await self.turn_repo.delete_by_session(session_id)
        await self.session.commit()

    async def delete_user(self, user_id: str):
        await self.memory_repo.delete_by_user(user_id)
        await self.turn_repo.delete_by_user(user_id)
        await self.session.commit()
