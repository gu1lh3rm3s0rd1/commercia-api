"""Assistant chat (Fase 11, RF08) — any logged-in user. The assistant sees every store the
user can access (and only those); conversations are visible to their owner only."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.ai.providers import get_provider
from app.ai.providers.base import LLMProvider
from app.core.deps import get_accessible_filial_ids, get_current_user
from app.db.session import get_db
from app.models.user import User
from app.modules.catalog.schemas import Page
from app.modules.chat import service
from app.modules.chat.schemas import (
    ChatRequest,
    ChatResponse,
    ConversationDetail,
    ConversationRead,
    MessageRead,
    ToolUsed,
)

router = APIRouter()


@router.post("", response_model=ChatResponse)
def chat(
    data: ChatRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    accessible: set[int] = Depends(get_accessible_filial_ids),
    provider: LLMProvider = Depends(get_provider),
) -> ChatResponse:
    """Ask a question. 503 when the assistant is unavailable (not configured, quota exhausted,
    model offline); 429 above the per-user message limit."""
    conversation, result, reply = service.ask(db, provider, user, accessible, data.message, data.conversation_id)
    return ChatResponse(
        conversation_id=conversation.id,
        answer=result.text,
        tools_used=[ToolUsed(name=r.call.name, args=r.call.args, ok="erro" not in r.result) for r in result.tool_runs],
        created_at=reply.created_at,
    )


@router.get("/conversations", response_model=Page[ConversationRead])
def list_conversations(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Page[ConversationRead]:
    rows, total = service.list_conversations(db, user, limit, offset)
    return Page(items=[ConversationRead.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ConversationDetail:
    conversation = service.get_conversation_or_404(db, conversation_id, user)
    return ConversationDetail(
        **ConversationRead.model_validate(conversation).model_dump(),
        messages=[MessageRead.model_validate(m) for m in service.messages(db, conversation.id)],
    )
