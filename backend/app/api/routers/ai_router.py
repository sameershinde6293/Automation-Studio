from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.infrastructure.database.database import get_db
from app.services.ai.orchestrator import ai_orchestrator

router = APIRouter(prefix="/ai", tags=["AI"])

class ChatRequest(BaseModel):
    conversation_id: int
    model_name: str
    message: str

@router.post("/chat")
async def chat(req: ChatRequest):
    try:
        response = await ai_orchestrator.chat(req.conversation_id, req.model_name, req.message)
        return {"response": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
