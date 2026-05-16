from pydantic import BaseModel


class SearchResult(BaseModel):
    content: str
    score: float
    session_id: str
    timestamp: str
    metadata: dict = {}


class SearchRequest(BaseModel):
    query: str
    session_id: str | None = None
    user_id: str | None = None
    limit: int = 10


class SearchResponse(BaseModel):
    results: list[SearchResult]
