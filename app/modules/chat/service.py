import re
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent import actions
from app.ai import orchestrator, prompts, tools
from app.core import audit
from app.ai.providers.base import LLMProvider
from app.core.config import get_settings
from app.models.agent import AgentAction
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
        message: str, conversation_id: int | None, other_stores: set[int] | None = None,
        ) -> tuple[ChatConversation, orchestrator.Answer, ChatMessage]:
    """Runs the question and stores question, tool calls and answer in ONE commit at the
    end — if the model is unavailable (LLMUnavailable → 503) nothing is saved.
    `accessible` = the user's stores whose plan has the Assistente IA (the tools see only those)."""
    if not accessible:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User has no store")
    _check_rate_limit(db, user)

    if conversation_id is not None:
        conversation = get_conversation_or_404(db, conversation_id, user)
        pending = _pending(db, conversation.id)
        if pending is not None:
            decided = _answer_proposal(db, user, accessible, conversation, pending, message)
            if decided is not None:  # a yes/no answer: decided by code, no model call
                return decided
        history = _history(db, conversation.id)
    else:
        conversation = ChatConversation(user_id=user.id, title=" ".join(message.split())[:120])
        history = []

    stores = tools.store_names(db, accessible)
    profiles = list(db.execute(select(Group.name).join(UserGroup, UserGroup.group_id == Group.id)
                               .where(UserGroup.user_id == user.id).order_by(Group.name)).scalars().all())
    if user.is_superuser:
        profiles.append("superusuário")
    ctx = tools.ToolContext(db=db, user=user, stores=stores,
                            outside_plan=tools.store_names(db, other_stores) if other_stores else None)
    result = orchestrator.answer(provider, ctx, prompts.system_prompt(user.full_name, profiles, stores),
                                 history, message)

    if conversation.id is None:
        db.add(conversation)
        db.flush()
    db.add(ChatMessage(conversation_id=conversation.id, role="user", content=message))
    for run in result.tool_runs:
        db.add(ChatMessage(conversation_id=conversation.id, role="tool", tool_name=run.call.name,
                           tool_args=run.call.args, tool_result=run.result))
    if ctx.proposal is not None:  # the agent proposed one action: store it, and always ask
        action_name, prepared = ctx.proposal
        result.pending = AgentAction(conversation_id=conversation.id, user_id=user.id, filial_id=prepared.filial_id,
                                     action_type=action_name, params=prepared.params, summary=prepared.summary,
                                     expires_at=datetime.now(timezone.utc) + actions.EXPIRES_AFTER)
        db.add(result.pending)
        if "responda sim ou não" not in result.text.lower():
            result.text = f"{result.text}\n\n**Proposta:** {prepared.summary}\n\n{CONFIRM_QUESTION}"
    reply = ChatMessage(conversation_id=conversation.id, role="assistant", content=result.text,
                        input_tokens=result.input_tokens, output_tokens=result.output_tokens)
    db.add(reply)
    conversation.updated_at = func.now()
    db.commit()
    db.refresh(reply)
    if result.pending is not None:
        db.refresh(result.pending)
    return conversation, result, reply


# ---------------------------------------------------------------------
# confirming a proposed action: "sim" / "não", recognised by code
# ---------------------------------------------------------------------

CONFIRM_QUESTION = "Posso fazer isso? Responda sim ou não."
_YES = re.compile(r"^\s*(sim|s|pode|pode sim|pode fazer|ok|confirmo|confirmado|fa[cç]a|yes)\s*[.!]*\s*$", re.IGNORECASE)
_NO = re.compile(r"^\s*(n[aã]o|n|cancela|cancelar|deixa|n[aã]o precisa|no)\s*[.!]*\s*$", re.IGNORECASE)


def classify_answer(message: str) -> str | None:
    """'yes', 'no', or None (anything else — then the proposal is discarded and the
    conversation goes on). Deliberately strict: only a clear yes runs an action."""
    if _YES.match(message):
        return "yes"
    if _NO.match(message):
        return "no"
    return None


def _pending(db: Session, conversation_id: int) -> AgentAction | None:
    return db.execute(select(AgentAction).where(AgentAction.conversation_id == conversation_id,
                                                AgentAction.status == "pending").with_for_update()).scalar_one_or_none()


def _answer_proposal(db: Session, user: User, accessible: set[int], conversation: ChatConversation,
                     pending: AgentAction, message: str):
    now = datetime.now(timezone.utc)
    kind = classify_answer(message)
    if kind is None:  # the user moved on: the proposal is dropped, the question goes to the model
        pending.status, pending.decided_at = "declined", now
        db.flush()
        return None
    if kind == "no":
        pending.status, pending.decided_at = "declined", now
        text = "Certo, não fiz nada."
    elif pending.expires_at < now:
        pending.status, pending.decided_at = "expired", now
        text = "Essa proposta expirou (vale por 15 minutos) e nada foi feito. Se ainda quiser, peça de novo."
    elif pending.filial_id not in accessible:
        pending.status, pending.decided_at = "failed", now
        text = "Não executei: você não tem mais acesso a essa loja (ou ela saiu do plano Hulk)."
    else:
        audit.set_source(db, "agent")  # the audit trail shows the change came through the agent
        try:
            result = actions.execute(db, user, pending.action_type, pending.params)
        except HTTPException as exc:
            db.rollback()
            pending = db.get(AgentAction, pending.id)
            pending.status, pending.decided_at, pending.result = "failed", now, {"erro": str(exc.detail)}
            text = f"Não consegui executar: {exc.detail}. Nada foi alterado."
        else:
            pending = db.get(AgentAction, pending.id)  # the services commit; reload before marking
            pending.status, pending.decided_at, pending.result = "executed", now, result
            text = f"Feito: {pending.summary}"
    db.add(ChatMessage(conversation_id=conversation.id, role="user", content=message))
    reply = ChatMessage(conversation_id=conversation.id, role="assistant", content=text)
    db.add(reply)
    conversation = db.get(ChatConversation, conversation.id)
    conversation.updated_at = func.now()
    db.commit()
    db.refresh(reply)
    return conversation, orchestrator.Answer(text=text), reply
