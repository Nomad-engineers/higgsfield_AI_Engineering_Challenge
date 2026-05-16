from pydantic import BaseModel, Field


class Message(BaseModel):
    role: str
    content: str
    name: str | None = None


class TurnCreate(BaseModel):
    session_id: str
    user_id: str | None = None
    messages: list[Message] = Field(..., min_length=1)
    timestamp: str
    metadata: dict | None = None


class TurnResponse(BaseModel):
    id: str
