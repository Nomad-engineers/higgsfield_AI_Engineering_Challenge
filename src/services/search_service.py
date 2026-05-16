from sqlalchemy.ext.asyncio import AsyncSession

from src.repositories.memory_repo import MemoryRepo


class SearchService:
    def __init__(self, session: AsyncSession):
        self.memory_repo = MemoryRepo(session)

    async def search(self, query: str, user_id: str, limit: int = 10) -> list[dict]:
        memories = await self.memory_repo.get_recent_by_user(user_id, limit=limit)
        results = []
        for m in memories:
            results.append({
                "id": str(m.id),
                "type": m.type,
                "key": m.key,
                "value": m.value,
                "confidence": m.confidence,
                "source_session": m.source_session,
                "created_at": m.created_at.isoformat() if m.created_at else "",
            })
        return results
