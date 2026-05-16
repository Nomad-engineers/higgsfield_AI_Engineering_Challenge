from pydantic import BaseModel


class Message(BaseModel):
    role: str
    content: str


class TurnCreate(BaseModel):
    session_id: str
    user_id: str | None = None
    messages: list[Message]
    timestamp: str
    metadata: dict | None = None


class TurnResponse(BaseModel):
    id: str
