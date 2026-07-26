from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain.models.ai.models import TokenUsage
from app.domain.repositories.ai.ai_repository import (
    AIModelRegistryCreate,
    AIModelRegistryUpdate,
    ConversationCreate,
    ConversationUpdate,
    MessageCreate,
    ai_model_repo,
    conversation_repo,
    message_repo,
    token_usage_repo,
)
from app.infrastructure.database.database import get_db
from app.services.ai.orchestrator import ai_orchestrator

router = APIRouter(prefix="/ai", tags=["AI"])


class ChatRequest(BaseModel):
    conversation_id: int
    model_name: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    options: Dict[str, Any] = Field(default_factory=dict)


@router.get("/providers", summary="Inspect configured AI providers")
def list_providers() -> List[Dict[str, Any]]:
    return ai_orchestrator.provider_info()


@router.post("/models", status_code=201, summary="Register an AI model")
def create_model(payload: AIModelRegistryCreate, db: Session = Depends(get_db)):
    if payload.provider not in ai_orchestrator.providers:
        raise ValidationError(
            f"Provider {payload.provider!r} is not configured.",
            details={"available": sorted(ai_orchestrator.providers)},
        )
    try:
        return ai_model_repo.create(db, payload)
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(f"AI model {payload.name!r} already exists.") from exc


@router.get("/models", summary="List AI models")
def list_models(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    active_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    query = db.query(ai_model_repo.model)
    if active_only:
        query = query.filter(ai_model_repo.model.is_active.is_(True))
    return query.offset(skip).limit(limit).all()


@router.get("/models/{model_id}", summary="Get an AI model")
def get_model(model_id: int, db: Session = Depends(get_db)):
    model = ai_model_repo.get(db, model_id)
    if not model:
        raise NotFoundError(f"AI model {model_id} not found.")
    return model


@router.put("/models/{model_id}", summary="Update an AI model")
def update_model(model_id: int, payload: AIModelRegistryUpdate, db: Session = Depends(get_db)):
    model = ai_model_repo.get(db, model_id)
    if not model:
        raise NotFoundError(f"AI model {model_id} not found.")
    if payload.provider and payload.provider not in ai_orchestrator.providers:
        raise ValidationError(
            f"Provider {payload.provider!r} is not configured.",
            details={"available": sorted(ai_orchestrator.providers)},
        )
    return ai_model_repo.update(db, model, payload)


@router.delete("/models/{model_id}", status_code=204, summary="Delete an AI model")
def delete_model(model_id: int, db: Session = Depends(get_db)) -> Response:
    if not ai_model_repo.delete(db, model_id):
        raise NotFoundError(f"AI model {model_id} not found.")
    return Response(status_code=204)


@router.post("/conversations", status_code=201, summary="Create a conversation")
def create_conversation(payload: ConversationCreate, db: Session = Depends(get_db)):
    return conversation_repo.create(db, payload)


@router.get("/conversations", summary="List conversations")
def list_conversations(skip: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    return conversation_repo.get_all(db, skip, limit)


@router.get("/conversations/{conversation_id}", summary="Get a conversation")
def get_conversation(conversation_id: int, include_messages: bool = Query(True), db: Session = Depends(get_db)):
    conversation = conversation_repo.get(db, conversation_id)
    if not conversation:
        raise NotFoundError(f"Conversation {conversation_id} not found.")
    if not include_messages:
        return conversation
    messages = (
        db.query(message_repo.model)
        .filter_by(conversation_id=conversation_id)
        .order_by(message_repo.model.created_at, message_repo.model.id)
        .all()
    )
    return {"conversation": conversation, "messages": messages}


@router.put("/conversations/{conversation_id}", summary="Update a conversation")
def update_conversation(conversation_id: int, payload: ConversationUpdate, db: Session = Depends(get_db)):
    conversation = conversation_repo.get(db, conversation_id)
    if not conversation:
        raise NotFoundError(f"Conversation {conversation_id} not found.")
    return conversation_repo.update(db, conversation, payload)


@router.delete("/conversations/{conversation_id}", status_code=204, summary="Delete a conversation and messages")
def delete_conversation(conversation_id: int, db: Session = Depends(get_db)) -> Response:
    conversation = conversation_repo.get(db, conversation_id)
    if not conversation:
        raise NotFoundError(f"Conversation {conversation_id} not found.")
    db.query(message_repo.model).filter_by(conversation_id=conversation_id).delete()
    conversation_repo.delete(db, conversation_id)
    return Response(status_code=204)


@router.post("/conversations/{conversation_id}/messages", status_code=201, summary="Append a conversation message")
def add_message(conversation_id: int, payload: MessageCreate, db: Session = Depends(get_db)):
    if conversation_id != payload.conversation_id:
        raise ValidationError("Path conversation_id must match payload conversation_id.")
    if not conversation_repo.get(db, conversation_id):
        raise NotFoundError(f"Conversation {conversation_id} not found.")
    return message_repo.create(db, payload)


@router.get("/conversations/{conversation_id}/messages", summary="List conversation messages")
def list_messages(conversation_id: int, db: Session = Depends(get_db)):
    if not conversation_repo.get(db, conversation_id):
        raise NotFoundError(f"Conversation {conversation_id} not found.")
    return (
        db.query(message_repo.model)
        .filter_by(conversation_id=conversation_id)
        .order_by(message_repo.model.created_at, message_repo.model.id)
        .all()
    )


@router.post("/chat", summary="Generate a chat completion")
async def chat(req: ChatRequest):
    return await ai_orchestrator.chat(req.conversation_id, req.model_name, req.message, **req.options)


@router.get("/usage", summary="List token usage rows")
def usage(model_name: Optional[str] = Query(None), db: Session = Depends(get_db)):
    query = db.query(token_usage_repo.model)
    if model_name:
        query = query.filter(token_usage_repo.model.model_name == model_name)
    return query.order_by(token_usage_repo.model.id.desc()).all()


@router.get("/usage/summary", summary="Aggregate token usage")
def usage_summary(model_name: Optional[str] = Query(None), db: Session = Depends(get_db)):
    query = db.query(
        TokenUsage.model_name,
        func.sum(TokenUsage.prompt_tokens).label("prompt_tokens"),
        func.sum(TokenUsage.completion_tokens).label("completion_tokens"),
        func.sum(TokenUsage.total_tokens).label("total_tokens"),
        func.count(TokenUsage.id).label("requests"),
    )
    if model_name:
        query = query.filter(TokenUsage.model_name == model_name)
    rows = query.group_by(TokenUsage.model_name).all()
    return [
        {
            "model_name": row.model_name,
            "prompt_tokens": row.prompt_tokens or 0,
            "completion_tokens": row.completion_tokens or 0,
            "total_tokens": row.total_tokens or 0,
            "requests": row.requests,
        }
        for row in rows
    ]
