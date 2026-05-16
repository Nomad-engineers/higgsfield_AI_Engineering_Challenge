from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.dependencies import get_db
from src.schemas.memory import MemoryListResponse, MemoryOut
from src.services.memory_service import MemoryService

router = APIRouter()


@router.get("/users/{user_id}/memories", response_model=MemoryListResponse)
async def get_user_memories(user_id: str, db: AsyncSession = Depends(get_db)):
    service = MemoryService(db)
    memories = await service.get_user_memories_with_history(user_id)

    superseded_by_map: dict[str, str] = {}
    for m in memories:
        if m.supersedes:
            superseded_by_map[str(m.supersedes)] = str(m.id)

    items = [
        MemoryOut(
            id=str(m.id),
            type=m.type,
            key=m.key,
            value=m.value,
            confidence=m.confidence,
            active=m.active,
            source_session=m.source_session,
            supersedes=str(m.supersedes) if m.supersedes else None,
            superseded_by=superseded_by_map.get(str(m.id)),
            created_at=m.created_at.isoformat() if m.created_at else "",
            updated_at=m.updated_at.isoformat() if m.updated_at else "",
        )
        for m in memories
    ]
    return MemoryListResponse(memories=items)
