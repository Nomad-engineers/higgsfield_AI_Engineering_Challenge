from pydantic import BaseModel


class SearchResult(BaseModel):
    id: str
    type: str
    key: str
    value: str
    confidence: float
    source_session: str
    created_at: str


class SearchRequest(BaseModel):
    query: str
    user_id: str
    limit: int = 10


class SearchResponse(BaseModel):
    results: list[SearchResult]
