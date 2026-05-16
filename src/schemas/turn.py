from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    content: str
    name: str | None = None


class TurnCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    user_id: str | None = None
    messages: list[Message] = Field(..., min_length=1)
    timestamp: str
    metadata: dict | None = None


class TurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
