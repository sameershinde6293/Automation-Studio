"""Structured logging tests: JSON output, redaction, rotation, correlation."""

import json
import logging

import pytest

from app.infrastructure.logging.logger import (
    ConsoleFormatter,
    JsonFormatter,
    RedactingFilter,
    get_logger,
    logger,
    redact,
    request_id_var,
    setup_logging,
)


class TestSetupLogging:
    def test_returns_named_logger(self):
        assert setup_logging().name == "creator_os"

    def test_module_singleton_exists(self):
        assert logger.name == "creator_os"
        assert logger.handlers

    def test_idempotent_without_force(self):
        first = setup_logging()
        count = len(first.handlers)
        second = setup_logging()
        assert second is first
        assert len(second.handlers) == count

    def test_force_rebuilds_handlers(self):
        log = setup_logging(force=True)
        assert len(log.handlers) >= 1

    def test_level_applied(self):
        log = setup_logging(level="DEBUG", force=True)
        assert log.level == logging.DEBUG
        setup_logging(level="INFO", force=True)

    def test_invalid_level_falls_back_to_info(self):
        log = setup_logging(level="NOT_A_LEVEL", force=True)
        assert log.level == logging.INFO

    def test_json_format_adds_json_formatter(self):
        log = setup_logging(fmt="json", force=True)
        assert isinstance(log.handlers[0].formatter, JsonFormatter)
        setup_logging(fmt="console", force=True)

    def test_file_handler_created(self, tmp_path):
        log_file = tmp_path / "logs" / "app.log"
        log = setup_logging(level="INFO", log_file=str(log_file), force=True)
        log.info("hello file")
        for handler in log.handlers:
            handler.flush()
        assert log_file.exists()
        assert "hello file" in log_file.read_text()
        setup_logging(force=True)

    def test_does_not_propagate_to_root(self):
        assert setup_logging(force=True).propagate is False


class TestGetLogger:
    def test_namespaced_child(self):
        assert get_logger("workflow").name == "creator_os.workflow"

    def test_empty_name_returns_root(self):
        assert get_logger().name == "creator_os"


class TestRedaction:
    @pytest.mark.parametrize(
        "text",
        [
            "key is sk-abcdefghijklmnop",
            'api_key="supersecret"',
            "Authorization: Bearer abcdef123456",
            'password="hunter2"',
            'token="abc123xyz"',
        ],
    )
    def test_secrets_are_masked(self, text):
        assert "REDACTED" in redact(text)

    def test_openai_key_masked(self):
        assert "abcdefghijklmnop" not in redact("sk-abcdefghijklmnop")

    def test_plain_text_untouched(self):
        assert redact("just a normal message") == "just a normal message"

    def test_empty_input(self):
        assert redact("") == ""

    def test_filter_redacts_record(self):
        record = logging.LogRecord(
            "n", logging.INFO, "p", 1, "sk-abcdefghijklmnop", None, None
        )
        RedactingFilter().filter(record)
        assert "REDACTED" in record.msg

    def test_filter_adds_request_id_attribute(self):
        record = logging.LogRecord("n", logging.INFO, "p", 1, "msg", None, None)
        RedactingFilter().filter(record)
        assert hasattr(record, "request_id")


class TestJsonFormatter:
    def _record(self, msg="hello", **extra):
        record = logging.LogRecord("creator_os", logging.INFO, "p", 1, msg, None, None)
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_emits_valid_json(self):
        payload = json.loads(JsonFormatter().format(self._record()))
        assert payload["message"] == "hello"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "creator_os"
        assert "ts" in payload

    def test_includes_request_id(self):
        payload = json.loads(JsonFormatter().format(self._record(request_id="abc")))
        assert payload["request_id"] == "abc"

    def test_includes_extra_fields(self):
        payload = json.loads(JsonFormatter().format(self._record(status_code=200)))
        assert payload["status_code"] == 200

    def test_non_serialisable_extra_is_stringified(self):
        payload = json.loads(JsonFormatter().format(self._record(obj=object())))
        assert isinstance(payload["obj"], str)

    def test_redacts_secrets(self):
        payload = json.loads(JsonFormatter().format(self._record("sk-abcdefghijklmnop")))
        assert "REDACTED" in payload["message"]

    def test_includes_exception(self):
        try:
            raise ValueError("bad")
        except ValueError:
            import sys

            record = logging.LogRecord(
                "creator_os", logging.ERROR, "p", 1, "err", None, sys.exc_info()
            )
        payload = json.loads(JsonFormatter().format(record))
        assert "ValueError" in payload["exception"]


class TestConsoleFormatter:
    def test_formats_message(self):
        record = logging.LogRecord("creator_os", logging.INFO, "p", 1, "hi", None, None)
        assert "hi" in ConsoleFormatter().format(record)

    def test_appends_request_id(self):
        record = logging.LogRecord("creator_os", logging.INFO, "p", 1, "hi", None, None)
        record.request_id = "rid-42"
        assert "request_id=rid-42" in ConsoleFormatter().format(record)

    def test_redacts(self):
        record = logging.LogRecord(
            "creator_os", logging.INFO, "p", 1, "sk-abcdefghijklmnop", None, None
        )
        assert "REDACTED" in ConsoleFormatter().format(record)


class TestRequestIdContextVar:
    def test_default_is_empty(self):
        assert request_id_var.get("") == ""

    def test_set_and_reset(self):
        token = request_id_var.set("abc")
        assert request_id_var.get() == "abc"
        request_id_var.reset(token)
        assert request_id_var.get("") == ""
