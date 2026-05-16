from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.dependencies import get_db
from src.schemas.turn import TurnCreate, TurnResponse
from src.services.memory_service import MemoryService

router = APIRouter()


@router.post("/turns", status_code=201, response_model=TurnResponse)
async def create_turn(body: TurnCreate, db: AsyncSession = Depends(get_db)):
    service = MemoryService(db)
    ts = datetime.fromisoformat(body.timestamp.replace("Z", "+00:00"))
    turn = await service.store_turn(
        session_id=body.session_id,
        user_id=body.user_id,
        messages=[m.model_dump() for m in body.messages],
        timestamp=ts,
        metadata=body.metadata,
    )
    await db.commit()
    return TurnResponse(id=str(turn.id))
