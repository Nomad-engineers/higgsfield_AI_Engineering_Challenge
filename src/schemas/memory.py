from pydantic import BaseModel


class MemoryOut(BaseModel):
    id: str
    type: str
    key: str
    value: str
    confidence: float
    active: bool
    source_session: str
    supersedes: str | None = None
    superseded_by: str | None = None
    created_at: str
    updated_at: str


class MemoryListResponse(BaseModel):
    memories: list[MemoryOut]
