from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.dependencies import get_db
from src.schemas.search import SearchRequest, SearchResponse, SearchResult
from src.services.search_service import SearchService

router = APIRouter()


@router.post("/search", response_model=SearchResponse)
async def search(body: SearchRequest, db: AsyncSession = Depends(get_db)):
    service = SearchService(db)
    raw_results = await service.search(
        query=body.query,
        user_id=body.user_id,
        session_id=body.session_id,
        limit=body.limit,
    )
    results = [SearchResult(**r) for r in raw_results]
    return SearchResponse(results=results)
