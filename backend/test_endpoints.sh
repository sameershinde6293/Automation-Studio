#!/bin/bash
set -e

echo "Starting FastAPI backend in the background..."
source venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000 &
PID=$!
sleep 3 # Wait for startup

echo "Testing Project Creation..."
curl -s -X POST "http://127.0.0.1:8000/api/projects/" -H "Content-Type: application/json" -d '{"name": "My First Project", "description": "Testing project creation"}'

echo -e "\nTesting AI Chat (Mock)..."
# We need an active conversation ID, assuming it's unconstrained for the mock provider right now or just pass conv ID 1.
curl -s -X POST "http://127.0.0.1:8000/api/ai/chat" -H "Content-Type: application/json" -d '{"conversation_id": 1, "model_name": "mock", "message": "Hello"}'

echo -e "\nTesting Workflow Creation..."
curl -s -X POST "http://127.0.0.1:8000/api/workflows/" -H "Content-Type: application/json" -d '{"name": "Test Workflow", "version": "1.0.0"}'

echo -e "\nTesting Workflow Execution (Assuming ID 1)..."
curl -s -X POST "http://127.0.0.1:8000/api/workflows/1/run"

echo -e "\nTesting Media Processing (Assuming ID 1)..."
curl -s -X POST "http://127.0.0.1:8000/api/media/1/process"

echo -e "\nKilling FastAPI backend..."
kill $PID
