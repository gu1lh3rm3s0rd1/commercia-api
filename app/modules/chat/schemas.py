from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    conversation_id: int | None = Field(None, description="omit to start a new conversation")


class ToolUsed(BaseModel):
    name: str
    args: dict[str, Any]
    ok: bool  # False when the tool answered with an error the model had to handle


class ChatResponse(BaseModel):
    conversation_id: int
    answer: str
    tools_used: list[ToolUsed]
    created_at: datetime


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    created_at: datetime
    updated_at: datetime


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str  # user | assistant | tool
    content: str
    tool_name: str | None
    tool_args: dict[str, Any] | None
    tool_result: dict[str, Any] | None
    created_at: datetime


class ConversationDetail(ConversationRead):
    messages: list[MessageRead]
