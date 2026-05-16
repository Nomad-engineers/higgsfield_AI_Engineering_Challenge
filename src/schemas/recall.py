from pydantic import BaseModel


class Citation(BaseModel):
    memory_id: str
    turn_id: str | None = None
    score: float
    snippet: str


class RecallRequest(BaseModel):
    query: str
    user_id: str
    session_id: str | None = None
    max_tokens: int = 512


class RecallResponse(BaseModel):
    context: str
    citations: list[Citation]
