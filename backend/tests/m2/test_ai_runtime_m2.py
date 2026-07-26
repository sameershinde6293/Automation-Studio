"""Milestone 2 AI runtime coverage: CRUD, provider introspection, usage and trimming."""

from __future__ import annotations

import pytest

from app.domain.repositories.ai.ai_repository import AIModelRegistryCreate, ConversationCreate, MessageCreate
from app.services.ai.orchestrator import ai_orchestrator, estimate_tokens


def test_ai_provider_introspection(api_client):
    body = api_client.get("/api/ai/providers").json()
    names = {p["name"] for p in body}
    assert {"mock", "openai", "local"} <= names
    assert next(p for p in body if p["name"] == "mock")["available"] is True


def test_model_registry_crud(api_client):
    created = api_client.post(
        "/api/ai/models", json={"name": "m2-mock", "provider": "mock", "model_type": "llm"}
    )
    assert created.status_code == 201
    model_id = created.json()["id"]
    assert api_client.post(
        "/api/ai/models", json={"name": "m2-mock", "provider": "mock", "model_type": "llm"}
    ).status_code == 409
    assert api_client.get(f"/api/ai/models/{model_id}").json()["name"] == "m2-mock"
    updated = api_client.put(f"/api/ai/models/{model_id}", json={"is_active": False}).json()
    assert updated["is_active"] is False
    assert api_client.delete(f"/api/ai/models/{model_id}").status_code == 204
    assert api_client.get(f"/api/ai/models/{model_id}").status_code == 404


def test_conversation_crud_and_messages(api_client):
    conv = api_client.post("/api/ai/conversations", json={"title": "M2"}).json()
    cid = conv["id"]
    msg = api_client.post(
        f"/api/ai/conversations/{cid}/messages",
        json={"conversation_id": cid, "role": "system", "content": "Be brief"},
    )
    assert msg.status_code == 201
    fetched = api_client.get(f"/api/ai/conversations/{cid}").json()
    assert fetched["conversation"]["title"] == "M2"
    assert fetched["messages"][0]["role"] == "system"
    assert api_client.put(f"/api/ai/conversations/{cid}", json={"title": "M2 updated"}).json()["title"] == "M2 updated"
    assert api_client.delete(f"/api/ai/conversations/{cid}").status_code == 204
    assert api_client.get(f"/api/ai/conversations/{cid}").status_code == 404


@pytest.mark.asyncio
async def test_chat_records_token_usage(db, monkeypatch):
    import app.services.ai.orchestrator as orch

    monkeypatch.setattr(orch, "SessionLocal", lambda: db)
    ai_model = AIModelRegistryCreate(name="chat-model", provider="mock", model_type="llm")
    from app.domain.repositories.ai.ai_repository import ai_model_repo, conversation_repo, token_usage_repo

    ai_model_repo.create(db, ai_model)
    conv = conversation_repo.create(db, ConversationCreate(title="runtime"))
    result = await ai_orchestrator.chat(conv.id, "chat-model", "hello")
    assert result["response"].startswith("Mock response")
    assert token_usage_repo.count(db) == 1


def test_chat_missing_model_has_no_message_side_effect(api_client):
    conv = api_client.post("/api/ai/conversations", json={"title": "No side effect"}).json()
    response = api_client.post(
        "/api/ai/chat",
        json={"conversation_id": conv["id"], "model_name": "missing-model", "message": "Hello"},
    )
    assert response.status_code == 404
    assert api_client.get(f"/api/ai/conversations/{conv['id']}/messages").json() == []


def test_chat_endpoint_usage_summary(api_client):
    api_client.post(
        "/api/ai/models", json={"name": "api-chat", "provider": "mock", "model_type": "llm"}
    ).json()
    conv = api_client.post("/api/ai/conversations", json={"title": "API"}).json()
    response = api_client.post(
        "/api/ai/chat", json={"conversation_id": conv["id"], "model_name": "api-chat", "message": "Hello"}
    )
    assert response.status_code == 200
    assert response.json()["conversation_id"] == conv["id"]
    usage = api_client.get("/api/ai/usage/summary?model_name=api-chat").json()
    assert usage[0]["requests"] == 1
    assert usage[0]["total_tokens"] > 0


@pytest.mark.parametrize("bad_payload", [
    {"name": "bad", "provider": "missing", "model_type": "llm"},
    {"name": "bad", "provider": "mock", "model_type": "invalid"},
    {"name": "", "provider": "mock", "model_type": "llm"},
])
def test_model_validation(api_client, bad_payload):
    assert api_client.post("/api/ai/models", json=bad_payload).status_code in {422, 400}


@pytest.mark.parametrize("role", ["system", "user", "assistant", "tool"])
def test_message_roles_are_accepted(api_client, role):
    conv = api_client.post("/api/ai/conversations", json={"title": role}).json()
    response = api_client.post(
        f"/api/ai/conversations/{conv['id']}/messages",
        json={"conversation_id": conv["id"], "role": role, "content": f"content {role}"},
    )
    assert response.status_code == 201


@pytest.mark.parametrize("max_messages", list(range(1, 61)))
def test_context_trimming_keeps_recent_messages(max_messages, monkeypatch):
    messages = [{"role": "system", "content": "policy"}] + [
        {"role": "user" if i % 2 else "assistant", "content": f"message {i}"} for i in range(120)
    ]
    trimmed, count = ai_orchestrator.trim_context(messages, max_messages=max_messages, max_tokens=10000)
    assert trimmed[0]["role"] == "system"
    assert len([m for m in trimmed if m["role"] != "system"]) <= max_messages
    assert count == len(messages) - len(trimmed)
    assert trimmed[-1]["content"] == "message 119"


@pytest.mark.parametrize("chars", ["x" * i for i in range(1, 101)])
def test_token_estimator_is_positive_and_monotonic(chars):
    assert estimate_tokens(chars) >= 1
    assert estimate_tokens(chars + "xxxx") >= estimate_tokens(chars)
