from pydantic import BaseModel, ConfigDict


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str
    score: float
    session_id: str
    timestamp: str
    metadata: dict = {}


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    session_id: str | None = None
    user_id: str | None = None
    limit: int = 10


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[SearchResult]
