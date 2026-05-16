import logging
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.dependencies import get_db
from src.schemas.turn import TurnCreate, TurnResponse
from src.services.memory_service import MemoryService

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/turns", status_code=201, response_model=TurnResponse)
async def create_turn(body: TurnCreate, db: AsyncSession = Depends(get_db)):
    service = MemoryService(db)
    ts = datetime.fromisoformat(body.timestamp.replace("Z", "+00:00"))

    # Phase 1: persist turn → commit (short transaction)
    turn = await service.persist_turn(
        session_id=body.session_id,
        user_id=body.user_id,
        messages=[m.model_dump() for m in body.messages],
        timestamp=ts,
        metadata=body.metadata,
    )
    turn_id = turn.id
    await db.commit()

    # Phase 2: extraction + memories → commit (separate transaction)
    if body.user_id:
        try:
            await service.extract_and_persist_memories(
                messages=[m.model_dump() for m in body.messages],
                user_id=body.user_id,
                session_id=body.session_id,
                turn_id=turn_id,
            )
            await db.commit()
        except Exception as e:
            logger.warning(f"Extraction failed for turn {turn_id}: {e}")
            await db.rollback()

    return TurnResponse(id=str(turn_id))
