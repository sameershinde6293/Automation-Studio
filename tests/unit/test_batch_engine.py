"""Unit tests for modules.batch_engine.BatchEngine.

Queue CRUD/order against the batch_queue table, sequential processing
with an injected processor callable (RULE 1 seam), retry policy from
app_settings, stop conditions, events, and stats. No FFmpeg involved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.service_container import ServiceContainer
from modules.batch_engine import BatchEngine

PROJECT = "proj-batch-1"
NOW = "2026-07-16 00:00:00"


def _container(project_root: Path, tmp_path: Path) -> ServiceContainer:
    return ServiceContainer.create_production_container(
        app_config={
            "database_path": str(tmp_path / "autopilot.db"),
            "schema_path": str(project_root / "database" / "schema.sql"),
            "config_folder": str(project_root / "config"),
            "cache_folder": str(tmp_path / "cache"),
            "log_folder": str(tmp_path / "logs"),
            "ffmpeg_path": "ffmpeg",
        },
        project_root=project_root,
    )


@pytest.fixture
def be(project_root: Path, tmp_path: Path) -> BatchEngine:
    return BatchEngine(_container(project_root, tmp_path))


@pytest.fixture
def project(be: BatchEngine, tmp_path: Path) -> str:
    folder = tmp_path / "proj"
    folder.mkdir(parents=True)
    be.db.db.execute(
        "INSERT INTO projects (id, title, channel_profile_id, genre,"
        " created_at, updated_at, project_folder_path)"
        " VALUES (?, ?, 'profile_default', 'dark_history', ?, ?, ?)",
        (PROJECT, "Batch Doc", NOW, NOW, str(folder)),
    )
    return PROJECT


def _ok_processor(item):
    return {
        "success": True,
        "data": {"output_file_path": f"/out/{item['id']}.mp4"},
        "error": None,
    }


def _fail_processor(item):
    return {"success": False, "data": {}, "error": "boom"}


# ------------------------------------------------------------------
# Queue management
# ------------------------------------------------------------------
def test_optional_module_and_settings_defaults(be: BatchEngine) -> None:
    assert be.is_optional_module() is True
    assert be._max_retries == 3  # app_settings.batch_retry_count
    assert be._stop_on_error is False  # app_settings.batch_stop_on_error


def test_add_with_project_uses_db_fields(
    be: BatchEngine, project: str
) -> None:
    result = be.add_to_queue(project_id=project, priority=2)
    assert result["success"] is True
    assert result["data"]["max_retries"] == 3
    row = be.get_item(result["data"]["id"])["data"]["item"]
    assert row["project_title"] == "Batch Doc"
    assert row["channel_profile_id"] == "profile_default"
    assert row["status"] == "queued"
    assert row["added_at"] is not None


def test_add_folder_only(be: BatchEngine, tmp_path: Path) -> None:
    folder = tmp_path / "loose_project"
    folder.mkdir()
    result = be.add_to_queue(project_folder_path=str(folder))
    assert result["success"] is True
    row = be.get_item(result["data"]["id"])["data"]["item"]
    assert row["project_id"] is None
    assert row["project_title"] == "loose_project"


def test_add_rejects_missing_project(be: BatchEngine) -> None:
    result = be.add_to_queue(project_id="no-such")
    assert result["success"] is False
    assert "Project not found" in result["error"]


def test_add_rejects_missing_folder(be: BatchEngine) -> None:
    result = be.add_to_queue(project_folder_path="/no/such/folder")
    assert result["success"] is False
    assert "does not exist" in result["error"]


def test_add_priority_clamped(be: BatchEngine, tmp_path: Path) -> None:
    folder = tmp_path / "f"
    folder.mkdir()
    low = be.add_to_queue(project_folder_path=str(folder), priority=99)
    high = be.add_to_queue(project_folder_path=str(folder), priority=0)
    assert low["data"]["priority"] == 10
    assert high["data"]["priority"] == 1
    assert any("clamped" in w for w in low["warnings"])


def test_list_queue_orders_by_priority(be: BatchEngine, tmp_path: Path) -> None:
    folder = tmp_path / "f"
    folder.mkdir()
    a = be.add_to_queue(project_folder_path=str(folder), priority=7)
    b = be.add_to_queue(project_folder_path=str(folder), priority=1)
    c = be.add_to_queue(project_folder_path=str(folder), priority=7)
    items = be.list_queue()["data"]["items"]
    assert [i["id"] for i in items] == [
        b["data"]["id"], a["data"]["id"], c["data"]["id"],
    ]
    queued = be.list_queue(status="queued")["data"]
    assert queued["count"] == 3
    assert be.list_queue(status="bogus")["success"] is False


def test_update_and_reprioritize_be_blocked_while_processing(
    be: BatchEngine, project: str
) -> None:
    item = be.add_to_queue(project_id=project)["data"]["id"]
    result = be.update_priority(item, 9)
    assert result["success"] is True
    assert be.get_item(item)["data"]["item"]["priority"] == 9
    be.db.db.execute(
        "UPDATE batch_queue SET status = 'processing' WHERE id = ?", (item,)
    )
    assert be.update_priority(item, 1)["success"] is False
    assert be.remove_from_queue(item)["success"] is False
    assert be.cancel_item(item)["success"] is False


def test_cancel_pause_resume(
    be: BatchEngine, project: str
) -> None:
    item = be.add_to_queue(project_id=project)["data"]["id"]
    assert be.pause_item(item)["success"] is True
    assert be.get_item(item)["data"]["item"]["status"] == "paused"
    assert be.resume_item(item)["success"] is True
    assert be.get_item(item)["data"]["item"]["status"] == "queued"
    assert be.cancel_item(item)["success"] is True
    assert be.get_item(item)["data"]["item"]["status"] == "cancelled"
    assert be.get_next_queued()["data"]["item"] is None


def test_retry_failed_and_clear_finished(
    be: BatchEngine, project: str, tmp_path: Path
) -> None:
    failed = be.add_to_queue(project_id=project)["data"]["id"]
    be.db.db.execute(
        "UPDATE batch_queue SET status = 'failed', retry_count = 3,"
        " error_message = 'x' WHERE id = ?",
        (failed,),
    )
    other = be.add_to_queue(project_id=project)["data"]["id"]
    be.db.db.execute(
        "UPDATE batch_queue SET status = 'completed' WHERE id = ?", (other,)
    )
    single = be.retry_failed(failed)
    assert single["success"] is True
    row = be.get_item(failed)["data"]["item"]
    assert row["status"] == "queued"
    assert row["retry_count"] == 0
    assert row["error_message"] is None
    cleared = be.clear_finished()
    assert cleared["data"]["cleared"] == 1
    assert be.get_queue_stats()["data"]["total"] == 1
    # failed-only retry refuses non-failed rows
    assert be.retry_failed(failed)["success"] is False


def test_get_next_queued_respects_priority(
    be: BatchEngine, tmp_path: Path
) -> None:
    folder = tmp_path / "f"
    folder.mkdir()
    low = be.add_to_queue(project_folder_path=str(folder), priority=8)
    top = be.add_to_queue(project_folder_path=str(folder), priority=1)
    nxt = be.get_next_queued()["data"]["item"]
    assert nxt["id"] == top["data"]["id"]
    be.cancel_item(top["data"]["id"])
    assert be.get_next_queued()["data"]["item"]["id"] == low["data"]["id"]


# ------------------------------------------------------------------
# Processing
# ------------------------------------------------------------------
def test_process_queue_requires_processor(be: BatchEngine) -> None:
    assert be.process_queue()["success"] is False
    assert "processor" in be.process_queue()["error"]
    assert be.process_queue(processor=123)["success"] is False


def test_process_empty_queue_succeeds(be: BatchEngine) -> None:
    result = be.process_queue(processor=_ok_processor)
    assert result["success"] is True
    assert result["data"]["processed"] == 0


def test_process_queue_success_path(be: BatchEngine, project: str) -> None:
    first = be.add_to_queue(project_id=project, priority=1)["data"]["id"]
    second = be.add_to_queue(project_id=project, priority=5)["data"]["id"]
    events = []
    for name in ("batch.item_started", "batch.item_completed",
                 "batch.queue_completed"):
        be.event_bus.subscribe(name, lambda d, n=name: events.append((n, d)))
    result = be.process_queue(processor=_ok_processor)
    assert result["success"] is True
    assert result["data"]["processed"] == 2
    assert result["data"]["completed"] == 2
    assert result["data"]["failed"] == 0
    rows = be.list_queue()["data"]["items"]
    for row in rows:
        assert row["status"] == "completed"
        assert row["started_at"] is not None
        assert row["completed_at"] is not None
        assert row["output_file_path"].endswith(".mp4")
    started_events = [e for e in events if e[0] == "batch.item_started"]
    assert len(started_events) == 2
    assert events[-1][0] == "batch.queue_completed"
    assert events[-1][1] == {
        "completed": 2, "failed": 0, "stopped_early": False,
    }
    _ = first, second


def test_process_next_single_item(be: BatchEngine, project: str) -> None:
    be.add_to_queue(project_id=project)
    be.add_to_queue(project_id=project)
    result = be.process_next(processor=_ok_processor)
    assert result["data"]["processed"] == 1
    assert be.get_queue_stats()["data"]["counts"]["queued"] == 1


def test_failure_retries_then_terminal(
    be: BatchEngine, project: str
) -> None:
    be._max_retries = 2
    attempts = []

    def _counting_fail(item):
        attempts.append(item["id"])
        return _fail_processor(item)

    item = be.add_to_queue(project_id=project)["data"]["id"]
    result = be.process_queue(processor=_counting_fail)
    assert result["data"]["failed"] == 1
    assert attempts == [item] * 3  # 1 initial + 2 retries
    row = be.get_item(item)["data"]["item"]
    assert row["status"] == "failed"
    assert row["retry_count"] == 3
    assert row["error_message"] == "boom"
    assert row["completed_at"] is not None


def test_retry_eventually_succeeds(be: BatchEngine, project: str) -> None:
    state = {"calls": 0}

    def _flaky(item):
        state["calls"] += 1
        if state["calls"] < 2:
            return _fail_processor(item)
        return _ok_processor(item)

    item = be.add_to_queue(project_id=project)["data"]["id"]
    started_at_before = be.get_item(item)["data"]["item"]["started_at"]
    result = be.process_queue(processor=_flaky)
    assert result["data"]["completed"] == 1
    assert state["calls"] == 2
    row = be.get_item(item)["data"]["item"]
    assert row["status"] == "completed"
    assert row["retry_count"] == 1
    assert started_at_before is None  # stamped during processing
    assert row["started_at"] is not None


def test_started_at_preserved_across_retries(
    be: BatchEngine, project: str
) -> None:
    stamps = []

    def _stamping(item):
        fresh = be.db.db.fetch_one(
            "SELECT started_at FROM batch_queue WHERE id = ?", (item["id"],)
        )
        stamps.append(fresh["started_at"])
        return _fail_processor(item)

    be._max_retries = 1
    item = be.add_to_queue(project_id=project)["data"]["id"]
    be.process_queue(processor=_stamping)
    assert len(stamps) == 2
    assert stamps[0] == stamps[1]  # COALESCE keeps the first attempt


def test_processor_exception_isolated(be: BatchEngine, project: str) -> None:
    def _raiser(item):
        raise RuntimeError("kaboom")

    be._max_retries = 0
    item = be.add_to_queue(project_id=project)["data"]["id"]
    result = be.process_queue(processor=_raiser)
    assert result["data"]["failed"] == 1
    row = be.get_item(item)["data"]["item"]
    assert "kaboom" in row["error_message"]


def test_processor_non_dict_response(be: BatchEngine, project: str) -> None:
    be._max_retries = 0
    item = be.add_to_queue(project_id=project)["data"]["id"]
    be.process_queue(processor=lambda item: "not-a-dict")
    row = be.get_item(item)["data"]["item"]
    assert row["status"] == "failed"
    assert "non-dict" in row["error_message"]


def test_stop_on_error_halts_queue(be: BatchEngine, project: str) -> None:
    be._max_retries = 0
    be._stop_on_error = True
    bad = be.add_to_queue(project_id=project, priority=1)["data"]["id"]
    good = be.add_to_queue(project_id=project, priority=5)["data"]["id"]
    result = be.process_queue(processor=_fail_processor)
    assert result["data"]["stopped_early"] is True
    assert be.get_item(bad)["data"]["item"]["status"] == "failed"
    assert be.get_item(good)["data"]["item"]["status"] == "queued"


def test_stop_file_halts_between_items(
    be: BatchEngine, project: str, tmp_path: Path
) -> None:
    stop = tmp_path / "STOP"

    def _stop_after_first(item):
        stop.write_text("halt", encoding="utf-8")
        return _ok_processor(item)

    be.add_to_queue(project_id=project)
    be.add_to_queue(project_id=project)
    result = be.process_queue(processor=_stop_after_first, stop_file=str(stop))
    assert result["data"]["processed"] == 1
    assert result["data"]["stopped_early"] is True
    assert be.get_queue_stats()["data"]["counts"]["queued"] == 1


def test_reentrancy_blocked(be: BatchEngine, project: str) -> None:
    be.add_to_queue(project_id=project)
    be._processing = True  # simulate an in-flight run
    result = be.process_queue(processor=_ok_processor)
    assert result["success"] is False
    assert "already being processed" in result["error"]


def test_disabled_module_blocks_add_and_process(
    be: BatchEngine, project: str
) -> None:
    be.set_enabled(False)
    assert be.add_to_queue(project_id=project)["success"] is False
    assert be.process_queue(processor=_ok_processor)["success"] is False


def test_queue_stats(be: BatchEngine, project: str, tmp_path: Path) -> None:
    be.add_to_queue(project_id=project)
    folder = tmp_path / "paused_one"
    folder.mkdir()
    paused = be.add_to_queue(project_folder_path=str(folder))["data"]["id"]
    be.pause_item(paused)
    stats = be.get_queue_stats()["data"]
    assert stats["counts"]["queued"] == 1
    assert stats["counts"]["paused"] == 1
    assert stats["total"] == 2
    assert stats["queued_estimated_min"] == 0.0
