from pydantic import BaseModel, ConfigDict


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: str | None = None
    score: float
    snippet: str


class RecallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    session_id: str
    user_id: str | None = None
    max_tokens: int = 512


class RecallResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context: str
    citations: list[Citation]
