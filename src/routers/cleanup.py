from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.dependencies import get_db
from src.services.memory_service import MemoryService

router = APIRouter()


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, db: AsyncSession = Depends(get_db)):
    service = MemoryService(db)
    await service.delete_session(session_id)
    return Response(status_code=204)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: str, db: AsyncSession = Depends(get_db)):
    service = MemoryService(db)
    await service.delete_user(user_id)
    return Response(status_code=204)
