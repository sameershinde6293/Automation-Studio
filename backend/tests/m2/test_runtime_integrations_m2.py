"""Additional M2 integration coverage for lifecycle, providers, scheduler and ffmpeg."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.infrastructure.scheduler.job_scheduler import JobScheduler
from app.services.ai.orchestrator import AIOrchestrator, ChatResult
from app.services.ai.providers.local_provider import OllamaProvider
from app.services.ai.providers.mock_provider import MockAIProvider
from app.services.ai.providers.openai_provider import OpenAIProvider
from app.services.media import ffmpeg as ffmpeg_mod
from app.services.media.storage import resolve_media_path


class FakeScheduler:
    def __init__(self):
        self.running = False
        self.jobs = []
        self.fail_start = False
        self.fail_shutdown = False

    def start(self):
        if self.fail_start:
            raise RuntimeError("boom")
        self.running = True

    def shutdown(self, wait=False):
        if self.fail_shutdown:
            raise RuntimeError("boom")
        self.running = False

    def add_job(self, func, trigger, **kwargs):
        job = SimpleNamespace(id=kwargs.get("id"), name=getattr(func, "__name__", "job"), next_run_time=None)
        self.jobs.append(job)
        return job

    def remove_job(self, job_id):
        self.jobs = [j for j in self.jobs if j.id != job_id]

    def get_jobs(self):
        return self.jobs


def test_scheduler_lifecycle_and_jobs(monkeypatch):
    scheduler = JobScheduler()
    fake = FakeScheduler()
    scheduler.scheduler = fake
    assert scheduler.start() is True
    assert scheduler.start() is False
    job = scheduler.add_interval_job(lambda: None, seconds=10, job_id="interval")
    assert job.id == "interval"
    assert scheduler.list_jobs()[0]["id"] == "interval"
    assert scheduler.remove_job("interval") is True
    assert scheduler.shutdown() is True
    assert scheduler.shutdown() is False


def test_scheduler_failure_paths():
    scheduler = JobScheduler()
    fake = FakeScheduler()
    fake.fail_start = True
    scheduler.scheduler = fake
    assert scheduler.start() is False
    fake.fail_start = False
    scheduler.start()
    fake.fail_shutdown = True
    assert scheduler.shutdown() is False


def test_lifespan_runs_startup_and_shutdown(monkeypatch):
    from app.main import create_app
    import app.main as main_mod

    events = []
    monkeypatch.setattr(main_mod.job_scheduler, "start", lambda: events.append("scheduler-start") or True)
    monkeypatch.setattr(main_mod.job_scheduler, "shutdown", lambda: events.append("scheduler-stop") or True)
    monkeypatch.setattr(main_mod.plugin_sdk, "trigger_hook", lambda hook: events.append(hook))
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/health/live").status_code == 200
    assert "scheduler-start" in events
    assert "scheduler-stop" in events


@pytest.mark.asyncio
async def test_mock_provider_stream_and_embed():
    provider = MockAIProvider()
    chunks = [chunk async for chunk in provider.generate_stream("m", [{"role": "user", "content": "hi"}])]
    assert "Mock " in chunks[0]
    assert await provider.embed("m", "text") == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_openai_provider_success_and_missing_key(monkeypatch):
    provider = OpenAIProvider()
    provider.api_key = "sk-test"

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 2}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    assert (await provider.generate("gpt", [{"role": "user", "content": "hi"}]))["content"] == "ok"
    with pytest.raises(NotImplementedError):
        await provider.generate_stream("gpt", [])
    with pytest.raises(NotImplementedError):
        await provider.embed("gpt", "hi")
    provider.api_key = ""
    with pytest.raises(ValueError):
        await provider.generate("gpt", [])


@pytest.mark.asyncio
async def test_ollama_provider_success_and_unimplemented(monkeypatch):
    provider = OllamaProvider()

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": "local"}, "prompt_eval_count": 3, "eval_count": 4}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    result = await provider.generate("llama", [{"role": "user", "content": "hi"}])
    assert result["usage"]["total_tokens"] == 7
    with pytest.raises(NotImplementedError):
        await provider.generate_stream("llama", [])
    with pytest.raises(NotImplementedError):
        await provider.embed("llama", "hi")


def test_chat_result_string_compatibility():
    result = ChatResult({"response": "hello"})
    assert "ell" in result
    assert str(result) == "hello"
    assert result == "hello"
    assert "response" in result


def test_orchestrator_validation_paths(db):
    orchestrator = AIOrchestrator()
    assert orchestrator._provider_available("missing") is False
    assert orchestrator._capabilities("missing") == []
    with pytest.raises(Exception):
        orchestrator.trim_context([], max_messages=0, max_tokens=1)


def test_ffprobe_success_error_and_invalid_json(monkeypatch, tmp_media_root):
    path = resolve_media_path("uploads/video.mp4")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x00\x00 ftypmp42data")
    monkeypatch.setattr(ffmpeg_mod, "tool_available", lambda binary: True)

    class Completed:
        def __init__(self, code, stdout="", stderr=""):
            self.returncode = code
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr(ffmpeg_mod.subprocess, "run", lambda *a, **k: Completed(0, '{"streams": []}'))
    assert ffmpeg_mod.probe_media("uploads/video.mp4")["available"] is True
    monkeypatch.setattr(ffmpeg_mod.subprocess, "run", lambda *a, **k: Completed(1, "", "bad"))
    assert ffmpeg_mod.probe_media("uploads/video.mp4")["available"] is False
    monkeypatch.setattr(ffmpeg_mod.subprocess, "run", lambda *a, **k: Completed(0, "not-json", ""))
    assert "Invalid ffprobe JSON" in ffmpeg_mod.probe_media("uploads/video.mp4")["error"]


def test_ffmpeg_poster_video_paths(monkeypatch, tmp_media_root):
    path = resolve_media_path("uploads/video.mp4")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x00\x00 ftypmp42data")
    monkeypatch.setattr(ffmpeg_mod, "tool_available", lambda binary: True)

    class Completed:
        returncode = 1
        stderr = "ffmpeg failed"

    monkeypatch.setattr(ffmpeg_mod.subprocess, "run", lambda *a, **k: Completed())
    result = ffmpeg_mod.generate_poster("uploads/video.mp4", "video")
    assert result["generated"] is False
    assert "ffmpeg failed" in result["error"]

    monkeypatch.setattr(ffmpeg_mod, "tool_available", lambda binary: False)
    assert ffmpeg_mod.generate_poster("uploads/video.mp4", "video")["generated"] is False
