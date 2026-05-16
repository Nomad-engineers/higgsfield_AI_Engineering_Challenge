from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.dependencies import get_db
from src.schemas.recall import Citation, RecallRequest, RecallResponse
from src.services.recall_service import RecallService

router = APIRouter()


@router.post("/recall", response_model=RecallResponse)
async def recall(body: RecallRequest, db: AsyncSession = Depends(get_db)):
    service = RecallService(db)
    context, raw_citations = await service.recall(
        query=body.query,
        user_id=body.user_id,
        session_id=body.session_id,
        max_tokens=body.max_tokens,
    )
    citations = [Citation(**c) for c in raw_citations]
    return RecallResponse(context=context, citations=citations)
