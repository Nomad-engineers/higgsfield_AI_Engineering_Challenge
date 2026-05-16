from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant", "tool"]
    content: str
    name: str | None = None


class TurnCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    user_id: str | None = None
    messages: list[Message] = Field(..., min_length=1)
    timestamp: str
    metadata: dict | None = None

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v):
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            raise ValueError("Invalid ISO-8601 timestamp")
        return v


class TurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
