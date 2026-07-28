"""Batch engine: sequential multi-project render queue.

Optional BaseModule (registry priority 15, CAN BE DISABLED: YES). Has no
File 07 module spec; built from the surrounding contract pieces:
the ``batch_queue`` table (schema.sql: priority 1-10, status enum
queued/processing/completed/failed/cancelled/paused, retry_count +
max_retries), app_settings (``batch_retry_count`` = 3,
``batch_stop_on_error`` = false), and File 12 "VERSION 1.0.0 / Batch
rendering queue" (plus its v1.2 "Create 5 videos in one batch"
short-form note).

RULE 1 and the stage order force a design seam: this module cannot
import file_parser/quality_checker/export_engine etc., so the actual
per-project render work is dependency-injected as a ``processor``
callable by the app orchestrator at runtime (wired in main.py, never
imported here). The queue engine owns bookkeeping only: states,
retries, stop conditions, events. Processing is strictly sequential in
v1 (File 12 puts multi-PC rendering in v2.0).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.service_container import BaseModule, ServiceContainer
from core.time_helper import utc_now_str

MODULE_NAME = "batch_engine"

_MIN_PRIORITY = 1  # schema: 1 (highest) to 10 (lowest)
_MAX_PRIORITY = 10
_DEFAULT_PRIORITY = 5
_QUEUE_STATUSES = (
    "queued", "processing", "completed", "failed", "cancelled", "paused",
)

# PHASE 9: hard cap on the per-item retry recursion (see _process_item).
# Far above any sane batch_retry_count, low enough to fail an item
# cleanly instead of exhausting the interpreter stack.
_MAX_RETRY_DEPTH = 50

Processor = Callable[[Dict[str, Any]], Dict[str, Any]]


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)


class BatchEngine(BaseModule):
    """Manage the batch_queue table and process it sequentially."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize engine; read retry/stop policy from app_settings."""
        super().__init__(container, MODULE_NAME)
        try:
            self._max_retries = int(self.config.get("batch_retry_count", 3))
        except (TypeError, ValueError):
            self._max_retries = 3
        self._stop_on_error = bool(self.config.get("batch_stop_on_error", False))
        self._processing = False

    def is_optional_module(self) -> bool:
        """Batch rendering is optional (registry required: false)."""
        return True

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------
    def add_to_queue(
        self,
        project_folder_path: Optional[str] = None,
        project_id: Optional[str] = None,
        project_title: Optional[str] = None,
        channel_profile_id: str = "default",
        priority: int = _DEFAULT_PRIORITY,
        notes: str = "",
    ) -> Dict[str, Any]:
        """Add a project (by id or folder path) to the render queue."""
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="batch_engine is disabled")
        warnings: List[str] = []

        title = project_title
        folder = project_folder_path
        if project_id:
            project = self.db.db.fetch_one(
                "SELECT id, title, project_folder_path, channel_profile_id"
                " FROM projects WHERE id = ?",
                (str(project_id),),
            )
            if project is None:
                return self.make_response(
                    False, error=f"Project not found: {project_id}"
                )
            folder = str(project.get("project_folder_path") or folder or "")
            title = title or str(project.get("title") or "Unknown")
            if project.get("channel_profile_id"):
                channel_profile_id = str(project["channel_profile_id"])
        if not folder:
            return self.make_response(
                False, error="project_folder_path (or project_id) is required"
            )
        if not Path(str(folder)).is_dir():
            if project_id:
                warnings.append(f"project folder missing on disk: {folder}")
            else:
                return self.make_response(
                    False, error=f"folder does not exist: {folder}"
                )
        if not title:
            title = Path(str(folder)).name or "Unknown"

        try:
            prio = int(priority)
        except (TypeError, ValueError):
            return self.make_response(
                False, error="priority must be an integer"
            )
        clamped = min(_MAX_PRIORITY, max(_MIN_PRIORITY, prio))
        if clamped != prio:
            warnings.append(f"priority clamped {prio} -> {clamped}")

        item_id = self.db.new_id()
        now = utc_now_str()
        self.db.db.execute(
            "INSERT INTO batch_queue (id, project_id, project_folder_path,"
            " project_title, channel_profile_id, priority, status, added_at,"
            " started_at, completed_at, output_file_path, error_message,"
            " retry_count, max_retries, estimated_duration_min, notes)"
            " VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, NULL, NULL, NULL, NULL,"
            " 0, ?, NULL, ?)",
            (
                item_id,
                str(project_id) if project_id else None,
                str(folder),
                str(title),
                str(channel_profile_id),
                clamped,
                now,
                self._max_retries,
                str(notes or ""),
            ),
        )
        self.event_bus.publish(
            "batch.item_added", {"id": item_id, "project_id": project_id}
        )
        self.log.info("Queued %s (%s) at priority %s", item_id, title, clamped)
        return self.make_response(
            True,
            {
                "id": item_id,
                "status": "queued",
                "priority": clamped,
                "max_retries": self._max_retries,
            },
            warnings=warnings,
            duration_ms=_ms(started),
        )

    def get_item(self, queue_id: str) -> Dict[str, Any]:
        """Get one queue row."""
        started = time.perf_counter()
        row = self.db.db.fetch_one(
            "SELECT * FROM batch_queue WHERE id = ?", (str(queue_id),)
        )
        if row is None:
            return self.make_response(
                False, error=f"Queue item not found: {queue_id}"
            )
        return self.make_response(
            True, {"item": row}, duration_ms=_ms(started)
        )

    def list_queue(self, status: Optional[str] = None) -> Dict[str, Any]:
        """List queue rows (priority then insertion order)."""
        started = time.perf_counter()
        if status:
            if status not in _QUEUE_STATUSES:
                return self.make_response(
                    False, error=f"unknown status: {status}"
                )
            rows = self.db.db.fetch_all(
                "SELECT * FROM batch_queue WHERE status = ?"
                " ORDER BY priority, added_at",
                (status,),
            )
        else:
            rows = self.db.db.fetch_all(
                "SELECT * FROM batch_queue ORDER BY priority, added_at"
            )
        return self.make_response(
            True, {"count": len(rows), "items": rows}, duration_ms=_ms(started)
        )

    def update_priority(self, queue_id: str, priority: int) -> Dict[str, Any]:
        """Change a queued item's priority (clamped 1-10)."""
        started = time.perf_counter()
        row = self.db.db.fetch_one(
            "SELECT id, status FROM batch_queue WHERE id = ?", (str(queue_id),)
        )
        if row is None:
            return self.make_response(
                False, error=f"Queue item not found: {queue_id}"
            )
        if row["status"] == "processing":
            return self.make_response(
                False, error="cannot reprioritize a processing item"
            )
        try:
            prio = int(priority)
        except (TypeError, ValueError):
            return self.make_response(False, error="priority must be an integer")
        prio = min(_MAX_PRIORITY, max(_MIN_PRIORITY, prio))
        self.db.db.execute(
            "UPDATE batch_queue SET priority = ? WHERE id = ?", (prio, queue_id)
        )
        return self.make_response(
            True, {"id": queue_id, "priority": prio}, duration_ms=_ms(started)
        )

    def remove_from_queue(self, queue_id: str) -> Dict[str, Any]:
        """Remove a queue row entirely (not while it is processing)."""
        started = time.perf_counter()
        row = self.db.db.fetch_one(
            "SELECT id, status FROM batch_queue WHERE id = ?", (str(queue_id),)
        )
        if row is None:
            return self.make_response(
                False, error=f"Queue item not found: {queue_id}"
            )
        if row["status"] == "processing":
            return self.make_response(
                False, error="cannot remove a processing item"
            )
        self.db.db.execute(
            "DELETE FROM batch_queue WHERE id = ?", (queue_id,)
        )
        return self.make_response(
            True, {"removed_id": queue_id}, duration_ms=_ms(started)
        )

    def _set_status(self, queue_id: str, status: str) -> Dict[str, Any]:
        row = self.db.db.fetch_one(
            "SELECT id, status FROM batch_queue WHERE id = ?", (str(queue_id),)
        )
        if row is None:
            return self.make_response(
                False, error=f"Queue item not found: {queue_id}"
            )
        if row["status"] == "processing":
            return self.make_response(
                False, error=f"cannot change a processing item to {status}"
            )
        self.db.db.execute(
            "UPDATE batch_queue SET status = ? WHERE id = ?", (status, queue_id)
        )
        return self.make_response(
            True, {"id": queue_id, "status": status}
        )

    def cancel_item(self, queue_id: str) -> Dict[str, Any]:
        """Cancel a queued/paused item."""
        started = time.perf_counter()
        result = self._set_status(queue_id, "cancelled")
        result["duration_ms"] = _ms(started)
        return result

    def pause_item(self, queue_id: str) -> Dict[str, Any]:
        """Pause a queued item (kept in the queue, skipped by the loop)."""
        started = time.perf_counter()
        result = self._set_status(queue_id, "paused")
        result["duration_ms"] = _ms(started)
        return result

    def resume_item(self, queue_id: str) -> Dict[str, Any]:
        """Resume a paused item back to queued."""
        started = time.perf_counter()
        row = self.db.db.fetch_one(
            "SELECT status FROM batch_queue WHERE id = ?", (str(queue_id),)
        )
        if row is None:
            return self.make_response(
                False, error=f"Queue item not found: {queue_id}"
            )
        if row["status"] != "paused":
            return self.make_response(
                False, error="only paused items can be resumed"
            )
        return self._set_status(queue_id, "queued")

    def retry_failed(self, queue_id: Optional[str] = None) -> Dict[str, Any]:
        """Requeue failed items (one, or all when no id is given)."""
        started = time.perf_counter()
        if queue_id:
            row = self.db.db.fetch_one(
                "SELECT status FROM batch_queue WHERE id = ?", (str(queue_id),)
            )
            if row is None:
                return self.make_response(
                    False, error=f"Queue item not found: {queue_id}"
                )
            if row["status"] != "failed":
                return self.make_response(
                    False, error="only failed items can be retried"
                )
            self.db.db.execute(
                "UPDATE batch_queue SET status = 'queued', retry_count = 0,"
                " error_message = NULL, completed_at = NULL WHERE id = ?",
                (queue_id,),
            )
            return self.make_response(
                True, {"requeued": 1}, duration_ms=_ms(started)
            )
        self.db.db.execute(
            "UPDATE batch_queue SET status = 'queued', retry_count = 0,"
            " error_message = NULL, completed_at = NULL"
            " WHERE status = 'failed'"
        )
        count = self.db.db.fetch_one(
            "SELECT COUNT(*) AS n FROM batch_queue WHERE status = 'queued'"
        )
        return self.make_response(
            True,
            {"requeued_queued_total": int((count or {}).get("n") or 0)},
            duration_ms=_ms(started),
        )

    def clear_finished(self) -> Dict[str, Any]:
        """Delete completed and cancelled rows (failed rows stay)."""
        started = time.perf_counter()
        count = self.db.db.fetch_one(
            "SELECT COUNT(*) AS n FROM batch_queue"
            " WHERE status IN ('completed', 'cancelled')"
        )
        self.db.db.execute(
            "DELETE FROM batch_queue WHERE status IN ('completed', 'cancelled')"
        )
        return self.make_response(
            True,
            {"cleared": int((count or {}).get("n") or 0)},
            duration_ms=_ms(started),
        )

    def get_next_queued(self) -> Dict[str, Any]:
        """Return the next queued row (priority, then insertion order)."""
        started = time.perf_counter()
        row = self.db.db.fetch_one(
            "SELECT * FROM batch_queue WHERE status = 'queued'"
            " ORDER BY priority, added_at LIMIT 1"
        )
        if row is None:
            return self.make_response(
                True, {"item": None}, duration_ms=_ms(started)
            )
        return self.make_response(
            True, {"item": row}, duration_ms=_ms(started)
        )

    def get_queue_stats(self) -> Dict[str, Any]:
        """Counts per status plus queued time estimate."""
        started = time.perf_counter()
        rows = self.db.db.fetch_all(
            "SELECT status, COUNT(*) AS n FROM batch_queue GROUP BY status"
        )
        counts = {status: 0 for status in _QUEUE_STATUSES}
        for row in rows:
            counts[str(row["status"])] = int(row["n"])
        estimate = self.db.db.fetch_one(
            "SELECT SUM(estimated_duration_min) AS total FROM batch_queue"
            " WHERE status = 'queued' AND estimated_duration_min IS NOT NULL"
        )
        return self.make_response(
            True,
            {
                "counts": counts,
                "total": sum(counts.values()),
                "queued_estimated_min": float(
                    (estimate or {}).get("total") or 0.0
                ),
            },
            duration_ms=_ms(started),
        )

    # ------------------------------------------------------------------
    # Processing (sequential; processor is dependency-injected - RULE 1)
    # ------------------------------------------------------------------
    def process_next(self, processor: Processor) -> Dict[str, Any]:
        """Process exactly one queued item (no-op success when empty)."""
        started = time.perf_counter()
        result = self._run(processor, single=True, stop_file=None)
        result["duration_ms"] = _ms(started)
        return result

    def process_queue(
        self,
        processor: Optional[Processor] = None,
        stop_file: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Process the queue until it is empty or a stop condition hits.

        ``processor`` is called as ``processor(item_row_dict)`` and must
        return a standard response dict (``success``/``data``/``error``);
        ``data.output_file_path`` (or ``output_path``) is recorded on the
        row. ``stop_file`` is an operator kill-switch: when the path
        exists, the loop stops *between* items. Failures retry up to
        ``max_retries``; with batch_stop_on_error the loop halts after
        the first terminal failure.
        """
        started = time.perf_counter()
        if processor is None:
            return self.make_response(
                False, error="no processor callable supplied"
            )
        if not callable(processor):
            return self.make_response(
                False, error="processor must be callable"
            )
        return self._run(processor, single=False, stop_file=stop_file, start=started)

    def _run(
        self,
        processor: Processor,
        single: bool,
        stop_file: Optional[str],
        start: Optional[float] = None,
    ) -> Dict[str, Any]:
        started = start if start is not None else time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="batch_engine is disabled")
        if self._processing:
            return self.make_response(
                False, error="queue is already being processed"
            )
        self._processing = True
        outcomes: List[Dict[str, Any]] = []
        completed = failed = 0
        stopped_early = False
        try:
            while True:
                if stop_file and Path(str(stop_file)).exists():
                    stopped_early = True
                    self.log.warning("Stop file requested: %s", stop_file)
                    break
                row = self.db.db.fetch_one(
                    "SELECT * FROM batch_queue WHERE status = 'queued'"
                    " ORDER BY priority, added_at LIMIT 1"
                )
                if row is None:
                    break
                outcome = self._process_item(row, processor)
                outcomes.append(outcome)
                if outcome["status"] == "completed":
                    completed += 1
                else:
                    failed += 1
                    if self._stop_on_error:
                        stopped_early = True
                        self.log.warning(
                            "batch_stop_on_error: halting after %s", row["id"]
                        )
                        break
                if single:
                    break
        finally:
            self._processing = False

        self.event_bus.publish(
            "batch.queue_completed",
            {"completed": completed, "failed": failed,
             "stopped_early": stopped_early},
        )
        return self.make_response(
            True,
            {
                "processed": len(outcomes),
                "completed": completed,
                "failed": failed,
                "stopped_early": stopped_early,
                "items": outcomes,
            },
            duration_ms=_ms(started),
        )

    def _process_item(
        self, row: Dict[str, Any], processor: Processor, depth: int = 0
    ) -> Dict[str, Any]:
        """Run one item through the retry loop; returns its outcome.

        PHASE 9: ``depth`` bounds the retry recursion. Retries recurse
        one frame per attempt, driven by a per-row ``max_retries`` that
        comes from the database — a corrupt or hand-edited row with a
        huge value used to be able to exhaust the stack instead of
        failing the item. Normal configurations (retry_count 3) never
        come close to the cap.
        """
        if depth > _MAX_RETRY_DEPTH:
            self.log.error(
                "Batch item %s exceeded the retry depth cap", row.get("id")
            )
            return {
                "id": row.get("id"),
                "status": "failed",
                "error": "retry limit exceeded",
            }
        queue_id = row["id"]
        now = utc_now_str()
        self.db.db.execute(
            "UPDATE batch_queue SET status = 'processing',"
            " started_at = COALESCE(started_at, ?) WHERE id = ?",
            (now, queue_id),
        )
        self.event_bus.publish(
            "batch.item_started",
            {"id": queue_id, "project_id": row.get("project_id")},
        )
        self.log.info(
            "Processing %s (%s) attempt %s",
            queue_id,
            row.get("project_title"),
            int(row.get("retry_count") or 0) + 1,
        )

        error: Optional[str] = None
        result_data: Dict[str, Any] = {}
        try:
            response = processor(dict(row))
            if isinstance(response, dict) and response.get("success"):
                result_data = dict(response.get("data") or {})
            else:
                error = (
                    str(response.get("error"))
                    if isinstance(response, dict)
                    else "processor returned a non-dict response"
                ) or "processor reported failure"
        except Exception as exc:  # noqa: BLE001 - isolate item failures
            self.log.exception("Processor crashed on %s", queue_id)
            error = f"processor exception: {exc}"

        finished = utc_now_str()
        if error is None:
            output = (
                result_data.get("output_file_path")
                or result_data.get("output_path")
            )
            self.db.db.execute(
                "UPDATE batch_queue SET status = 'completed',"
                " completed_at = ?, output_file_path = ?,"
                " error_message = NULL WHERE id = ?",
                (finished, str(output) if output else None, queue_id),
            )
            self.event_bus.publish(
                "batch.item_completed",
                {"id": queue_id, "output_file_path": output},
            )
            return {"id": queue_id, "status": "completed", "error": None}

        retry_count = int(row.get("retry_count") or 0) + 1
        max_retries = int(row.get("max_retries") or self._max_retries or 0)
        if retry_count > max_retries:
            self.db.db.execute(
                "UPDATE batch_queue SET status = 'failed', retry_count = ?,"
                " error_message = ?, completed_at = ? WHERE id = ?",
                (retry_count, error, finished, queue_id),
            )
            self.event_bus.publish(
                "batch.item_failed", {"id": queue_id, "error": error}
            )
            return {"id": queue_id, "status": "failed", "error": error}
        self.db.db.execute(
            "UPDATE batch_queue SET status = 'queued', retry_count = ?,"
            " error_message = ? WHERE id = ?",
            (retry_count, error, queue_id),
        )
        self.event_bus.publish(
            "batch.item_retrying",
            {"id": queue_id, "retry_count": retry_count, "error": error},
        )
        # Re-run happens on the next loop turn (fresh row fetch).
        refreshed = self.db.db.fetch_one(
            "SELECT * FROM batch_queue WHERE id = ?", (queue_id,)
        )
        follow_up = self._process_item(
            refreshed, processor, depth + 1
        ) if refreshed else {
            "id": queue_id,
            "status": "failed",
            "error": "queue row disappeared mid-retry",
        }
        return follow_up
