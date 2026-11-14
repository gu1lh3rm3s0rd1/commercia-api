from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai import orchestrator, prompts, tools
from app.ai.providers.base import LLMProvider
from app.core.config import get_settings
from app.models.chat import ChatConversation, ChatMessage
from app.models.user import Group, User, UserGroup

HISTORY_MESSAGES = 10  # earlier user/assistant messages sent as context


def get_conversation_or_404(db: Session, conversation_id: int, user: User) -> ChatConversation:
    conversation = db.get(ChatConversation, conversation_id)
    if conversation is None or conversation.user_id != user.id:  # only the owner, not even admins
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return conversation


def list_conversations(db: Session, user: User, limit: int, offset: int) -> tuple[list[ChatConversation], int]:
    base = select(ChatConversation).where(ChatConversation.user_id == user.id)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(base.order_by(ChatConversation.updated_at.desc(), ChatConversation.id.desc())
                      .limit(limit).offset(offset)).scalars().all()
    return list(rows), total


def messages(db: Session, conversation_id: int) -> list[ChatMessage]:
    return list(db.execute(select(ChatMessage).where(ChatMessage.conversation_id == conversation_id)
                           .order_by(ChatMessage.id)).scalars().all())


def _check_rate_limit(db: Session, user: User) -> None:
    limit = get_settings().chat_rate_limit_per_minute
    recent = db.scalar(
        select(func.count()).select_from(ChatMessage)
        .join(ChatConversation, ChatConversation.id == ChatMessage.conversation_id)
        .where(ChatConversation.user_id == user.id, ChatMessage.role == "user",
               ChatMessage.created_at >= datetime.now(timezone.utc) - timedelta(minutes=1))
    ) or 0
    if recent >= limit:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail=f"Limite de {limit} mensagens por minuto atingido. Aguarde um instante.")


def _history(db: Session, conversation_id: int) -> list[tuple[str, str]]:
    rows = db.execute(
        select(ChatMessage.role, ChatMessage.content)
        .where(ChatMessage.conversation_id == conversation_id, ChatMessage.role.in_(("user", "assistant")))
        .order_by(ChatMessage.id.desc()).limit(HISTORY_MESSAGES)
    ).all()
    return [(r.role, r.content) for r in reversed(rows)]


def ask(db: Session, provider: LLMProvider, user: User, accessible: set[int],
        message: str, conversation_id: int | None) -> tuple[ChatConversation, orchestrator.Answer, ChatMessage]:
    """Runs the question and stores question, tool calls and answer in ONE commit at the
    end — if the model is unavailable (LLMUnavailable → 503) nothing is saved."""
    if not accessible:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User has no store")
    _check_rate_limit(db, user)

    if conversation_id is not None:
        conversation = get_conversation_or_404(db, conversation_id, user)
        history = _history(db, conversation.id)
    else:
        conversation = ChatConversation(user_id=user.id, title=" ".join(message.split())[:120])
        history = []

    stores = tools.store_names(db, accessible)
    profiles = list(db.execute(select(Group.name).join(UserGroup, UserGroup.group_id == Group.id)
                               .where(UserGroup.user_id == user.id).order_by(Group.name)).scalars().all())
    if user.is_superuser:
        profiles.append("superusuário")
    ctx = tools.ToolContext(db=db, user=user, stores=stores)
    result = orchestrator.answer(provider, ctx, prompts.system_prompt(user.full_name, profiles, stores),
                                 history, message)

    if conversation.id is None:
        db.add(conversation)
        db.flush()
    db.add(ChatMessage(conversation_id=conversation.id, role="user", content=message))
    for run in result.tool_runs:
        db.add(ChatMessage(conversation_id=conversation.id, role="tool", tool_name=run.call.name,
                           tool_args=run.call.args, tool_result=run.result))
    reply = ChatMessage(conversation_id=conversation.id, role="assistant", content=result.text,
                        input_tokens=result.input_tokens, output_tokens=result.output_tokens)
    db.add(reply)
    conversation.updated_at = func.now()
    db.commit()
    db.refresh(reply)
    return conversation, result, reply
