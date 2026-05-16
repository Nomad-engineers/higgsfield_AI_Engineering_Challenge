from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class MemoryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    type: str
    key: str
    value: str
    confidence: float
    active: bool
    source_session: str
    source_turn: str | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    created_at: str
    updated_at: str


class MemoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memories: list[MemoryOut]
