"""M4 node library tests: control, AI, network, data/script/IO and media nodes.

Nodes that reach outside the process (SMTP, FFmpeg, Node.js, real providers) are
exercised through their guarded paths — disabled flags, dry-run behaviour and
error classification — rather than by contacting anything external.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.errors import SecurityError, ValidationError
from app.services.workflow.nodes import NODE_LIBRARY, iter_registrations
from app.services.workflow.nodes.control_nodes import (
    ConditionNode,
    DelayNode,
    EndNode,
    LoopNode,
    StartNode,
    VariableNode,
)
from app.services.workflow.nodes.data_nodes import (
    DatabaseNode,
    EmailNode,
    FileNode,
    FolderNode,
    JavaScriptNode,
    PythonNode,
)
from app.services.workflow.nodes.media_nodes import (
    FFmpegNode,
    MediaProcessingNode,
    STTNode,
    TTSNode,
)
from app.services.workflow.nodes.network_nodes import HTTPRequestNode, WebhookNode
from app.services.workflow.runtime import (
    NodeContext,
    NodeErrorCode,
    NodeExecutionError,
    NodeResult,
)


class FakeNode:
    """Minimal stand-in for a Node ORM row."""

    def __init__(self, config=None, name="n", node_id=1, node_type="test"):
        self.id = node_id
        self.name = name
        self.node_type = node_type
        self.config = config or {}


async def run_node(executor, config=None, context=None):
    context = context if context is not None else NodeContext()
    return await executor.execute(FakeNode(config), context)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
class TestRegistration:
    def test_library_covers_every_editor_type(self):
        editor_types = {
            "start", "end", "aiChat", "aiCompletion", "prompt", "variable",
            "condition", "loop", "delay", "httpRequest", "webhook", "python",
            "javascript", "database", "email", "file", "folder",
            "imageGeneration", "tts", "stt", "ffmpeg", "mediaProcessing",
        }
        assert editor_types <= set(NODE_LIBRARY)

    def test_aliases_resolve_to_the_same_instance(self):
        from app.services.workflow.executors import executor_registry

        assert executor_registry.get_executor("httpRequest") is (
            executor_registry.get_executor("http_request_node")
        )

    def test_m1_node_types_are_not_hijacked(self):
        """Pre-existing M1 types keep their original executors.

        ``http_request`` and ``delay`` already existed before M4, so saved V1.0
        and V1.1 workflows must keep resolving to the executor they were built
        against rather than silently switching implementation.
        """
        from app.services.workflow.executors import (
            HttpRequestExecutor,
            executor_registry,
        )

        assert isinstance(
            executor_registry.get_executor("http_request"), HttpRequestExecutor
        )

    def test_every_node_declares_a_schema(self):
        for node_type, executor in NODE_LIBRARY.items():
            described = executor.describe(node_type)
            assert described["label"], f"{node_type} has no label"
            assert "inputs" in described["schema"]
            assert "outputs" in described["schema"]

    def test_registration_list_has_no_duplicates(self):
        names = [name for name, _executor, _alias in iter_registrations()]
        assert len(names) == len(set(names))


# --------------------------------------------------------------------------- #
# Control nodes
# --------------------------------------------------------------------------- #
class TestControlNodes:
    async def test_start_seeds_variables(self):
        context = NodeContext()
        result = await run_node(
            StartNode(), {"name": "Main", "variables": {"env": "prod"}}, context
        )
        assert result["started"] is True
        assert context.get_variable("env") == "prod"

    async def test_start_does_not_clobber_caller_inputs(self):
        context = NodeContext(variables={"env": "from-caller"})
        await run_node(StartNode(), {"variables": {"env": "from-config"}}, context)
        assert context.get_variable("env") == "from-caller"

    async def test_end_captures_final_output(self):
        context = NodeContext(variables={"answer": 42})
        result = await run_node(EndNode(), {"output": "{{ vars.answer }}"}, context)
        assert result["completed"] is True
        assert result["output"] == "42"
        assert context.get_variable("__result__") == "42"

    async def test_variable_set_and_get(self):
        context = NodeContext()
        await run_node(VariableNode(), {"name": "x", "value": "5"}, context)
        assert context.get_variable("x") == "5"

        result = await run_node(VariableNode(), {"name": "x", "operation": "get"}, context)
        assert result["value"] == "5"

    async def test_variable_increment_and_append(self):
        context = NodeContext(variables={"count": 1, "items": ["a"]})
        result = await run_node(
            VariableNode(), {"name": "count", "operation": "increment", "value": 2},
            context,
        )
        assert result["value"] == 3

        result = await run_node(
            VariableNode(), {"name": "items", "operation": "append", "value": "b"},
            context,
        )
        assert result["value"] == ["a", "b"]

    async def test_variable_delete(self):
        context = NodeContext(variables={"gone": 1})
        await run_node(VariableNode(), {"name": "gone", "operation": "delete"}, context)
        assert context.get_variable("gone") is None

    async def test_variable_requires_a_name(self):
        with pytest.raises(ValidationError):
            await run_node(VariableNode(), {"name": "  "})

    @pytest.mark.parametrize(
        "left,operator,right,expected",
        [
            (5, ">", 3, True),
            (2, ">", 3, False),
            ("abc", "contains", "b", True),
            ("abc", "not_contains", "z", True),
            ("hello", "starts_with", "he", True),
            ("hello", "ends_with", "lo", True),
            ("", "is_empty", None, True),
            ("x", "is_not_empty", None, True),
            (1, "==", 1, True),
            (1, "!=", 2, True),
        ],
    )
    def test_condition_operators(self, left, operator, right, expected):
        assert ConditionNode.evaluate(operator, left, right) is expected

    def test_condition_rejects_unknown_operator(self):
        with pytest.raises(ValidationError):
            ConditionNode.evaluate("~~", 1, 2)

    async def test_condition_returns_branch_label(self):
        context = NodeContext()
        node = ConditionNode()
        result = await node.execute_result(
            FakeNode({"left": "10", "operator": ">", "right": "2"}), context
        )
        assert isinstance(result, NodeResult)
        assert result.output["result"] is True
        assert result.branches == ["true"]

    async def test_condition_custom_labels(self):
        context = NodeContext()
        result = await ConditionNode().execute_result(
            FakeNode(
                {
                    "left": "0",
                    "operator": "truthy",
                    "true_label": "yes",
                    "false_label": "no",
                }
            ),
            context,
        )
        assert result.branches == ["no"]

    async def test_loop_collect_renders_per_item(self):
        context = NodeContext()
        result = await run_node(
            LoopNode(),
            {"items": ["a", "b"], "template": "item-{{ loop.item }}", "mode": "collect"},
            context,
        )
        assert result["results"] == ["item-a", "item-b"]
        assert result["count"] == 2

    async def test_loop_from_count(self):
        result = await run_node(LoopNode(), {"count": 3})
        assert result["items"] == [0, 1, 2]

    async def test_loop_parses_json_array_string(self):
        result = await run_node(LoopNode(), {"items": "[1, 2, 3]"})
        assert result["items"] == [1, 2, 3]

    async def test_loop_parses_csv_string(self):
        result = await run_node(LoopNode(), {"items": "a, b, c"})
        assert result["items"] == ["a", "b", "c"]

    async def test_loop_resolves_a_reference_to_a_list(self):
        context = NodeContext()
        context.record_output(3, "Fetch", {"rows": [10, 20]})
        result = await run_node(LoopNode(), {"items": "{{ Fetch.rows }}"}, context)
        assert result["items"] == [10, 20]

    async def test_loop_is_capped(self, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "WORKFLOW_MAX_LOOP_ITERATIONS", 5)
        result = await run_node(LoopNode(), {"count": 100})
        assert result["count"] == 5
        assert result["truncated"] is True

    async def test_delay_sleeps(self):
        loop = asyncio.get_event_loop()
        started = loop.time()
        result = await run_node(DelayNode(), {"seconds": 0.05})
        assert result["slept_seconds"] == 0.05
        assert loop.time() - started >= 0.04

    async def test_delay_wakes_early_on_stop(self):
        context = NodeContext()
        context.cancel_event = asyncio.Event()
        context.cancel_event.set()

        loop = asyncio.get_event_loop()
        started = loop.time()
        result = await run_node(DelayNode(), {"seconds": 5}, context)
        assert result["interrupted"] is True
        assert loop.time() - started < 1.0

    async def test_delay_rejects_negative_duration(self):
        """The schema rejects a negative delay rather than silently clamping."""
        with pytest.raises(ValidationError):
            await run_node(DelayNode(), {"seconds": -10})

    async def test_delay_rejects_absurd_duration(self):
        with pytest.raises(ValidationError):
            await run_node(DelayNode(), {"seconds": 999999})


# --------------------------------------------------------------------------- #
# Network nodes
# --------------------------------------------------------------------------- #
class TestNetworkNodes:
    async def test_http_blocks_private_addresses(self):
        with pytest.raises(SecurityError):
            await run_node(HTTPRequestNode(), {"url": "http://127.0.0.1/admin"})

    async def test_http_blocks_non_http_schemes(self):
        with pytest.raises(SecurityError):
            await run_node(HTTPRequestNode(), {"url": "file:///etc/passwd"})

    async def test_http_blocks_cloud_metadata(self):
        with pytest.raises(SecurityError):
            await run_node(
                HTTPRequestNode(), {"url": "http://metadata.google.internal/x"}
            )

    async def test_http_rejects_unsupported_method(self):
        with pytest.raises(ValidationError):
            await run_node(
                HTTPRequestNode(), {"url": "https://example.com", "method": "TRACE"}
            )

    async def test_http_requires_a_url(self):
        with pytest.raises(ValidationError):
            await run_node(HTTPRequestNode(), {})

    async def test_http_rejects_non_object_headers(self):
        with pytest.raises(ValidationError):
            await run_node(
                HTTPRequestNode(),
                {"url": "https://example.com", "headers": "not-an-object"},
            )

    async def test_webhook_blocks_private_addresses(self):
        with pytest.raises(SecurityError):
            await run_node(WebhookNode(), {"url": "http://10.0.0.1/hook"})

    async def test_webhook_rejects_bad_method(self):
        with pytest.raises(ValidationError):
            await run_node(
                WebhookNode(), {"url": "https://example.com", "method": "GET"}
            )


# --------------------------------------------------------------------------- #
# Script / data nodes (disabled by default)
# --------------------------------------------------------------------------- #
class TestScriptNodes:
    async def test_python_disabled_by_default(self):
        with pytest.raises(NodeExecutionError) as excinfo:
            await run_node(PythonNode(), {"code": "result = 1"})
        assert excinfo.value.code == NodeErrorCode.DISABLED

    async def test_python_runs_when_enabled(self, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "ALLOW_PYTHON_EXECUTOR", True)
        result = await run_node(
            PythonNode(), {"code": "result = sum(input_values)",
                           "inputs": {"input_values": [1, 2, 3]}}
        )
        assert result["result"] == 6

    async def test_python_captures_stdout(self, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "ALLOW_PYTHON_EXECUTOR", True)
        result = await run_node(PythonNode(), {"code": "print('hi')\nresult = 1"})
        assert "hi" in result["stdout"]

    @pytest.mark.parametrize(
        "code",
        [
            "import os",
            "__import__('os')",
            "open('/etc/passwd')",
            "eval('1+1')",
            "exec('x=1')",
            "globals()",
            "getattr(object, 'x')",
            "os.system('ls')",
        ],
    )
    async def test_python_rejects_dangerous_code(self, monkeypatch, code):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "ALLOW_PYTHON_EXECUTOR", True)
        with pytest.raises(SecurityError):
            await run_node(PythonNode(), {"code": code})

    async def test_python_syntax_error_is_validation(self, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "ALLOW_PYTHON_EXECUTOR", True)
        with pytest.raises(ValidationError):
            await run_node(PythonNode(), {"code": "result = ("})

    async def test_python_runtime_error_is_classified(self, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "ALLOW_PYTHON_EXECUTOR", True)
        with pytest.raises(NodeExecutionError) as excinfo:
            await run_node(PythonNode(), {"code": "result = 1/0"})
        assert excinfo.value.code == NodeErrorCode.RUNTIME

    async def test_python_requires_code(self, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "ALLOW_PYTHON_EXECUTOR", True)
        with pytest.raises(ValidationError):
            await run_node(PythonNode(), {"code": "   "})

    async def test_javascript_disabled_by_default(self):
        with pytest.raises(NodeExecutionError) as excinfo:
            await run_node(JavaScriptNode(), {"code": "result = 1"})
        assert excinfo.value.code == NodeErrorCode.DISABLED

    async def test_database_disabled_by_default(self):
        with pytest.raises(NodeExecutionError) as excinfo:
            await run_node(DatabaseNode(), {"query": "SELECT 1"})
        assert excinfo.value.code == NodeErrorCode.DISABLED

    async def test_database_blocks_writes_without_flag(self, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "ALLOW_DATABASE_EXECUTOR", True)
        with pytest.raises(SecurityError):
            await run_node(DatabaseNode(), {"query": "DELETE FROM workflows"})

    async def test_database_blocks_stacked_statements(self, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "ALLOW_DATABASE_EXECUTOR", True)
        with pytest.raises(SecurityError):
            await run_node(
                DatabaseNode(), {"query": "SELECT 1; DROP TABLE workflows"}
            )

    async def test_database_select_returns_rows(self, monkeypatch, session_factory):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "ALLOW_DATABASE_EXECUTOR", True)
        monkeypatch.setattr(
            "app.infrastructure.database.database.SessionLocal", session_factory
        )
        result = await run_node(DatabaseNode(), {"query": "SELECT 1 AS one"})
        assert result["rows"] == [{"one": 1}]


class TestEmailNode:
    async def test_dry_run_when_smtp_unconfigured(self, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "SMTP_HOST", "")
        result = await run_node(
            EmailNode(),
            {"to": ["a@example.com"], "subject": "Hi", "body": "Body"},
        )
        assert result["sent"] is False
        assert result["dry_run"] is True

    async def test_rejects_invalid_address(self):
        with pytest.raises(ValidationError):
            await run_node(
                EmailNode(), {"to": ["not-an-email"], "subject": "s", "body": "b"}
            )

    async def test_requires_a_recipient(self):
        with pytest.raises(ValidationError):
            await run_node(EmailNode(), {"to": [], "subject": "s", "body": "b"})

    async def test_accepts_comma_separated_recipients(self, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "SMTP_HOST", "")
        result = await run_node(
            EmailNode(),
            {"to": "a@example.com, b@example.com", "subject": "s", "body": "b"},
        )
        assert len(result["recipients"]) == 2

    async def test_templates_are_rendered(self, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "SMTP_HOST", "")
        context = NodeContext(variables={"who": "Ada"})
        result = await run_node(
            EmailNode(),
            {"to": ["a@example.com"], "subject": "Hi {{ vars.who }}", "body": "b"},
            context,
        )
        assert result["subject"] == "Hi Ada"


class TestFileNodes:
    async def test_write_then_read(self, tmp_media_root):
        await run_node(
            FileNode(),
            {"path": "notes/hello.txt", "operation": "write", "content": "hi there"},
        )
        result = await run_node(
            FileNode(), {"path": "notes/hello.txt", "operation": "read"}
        )
        assert result["content"] == "hi there"

    async def test_append(self, tmp_media_root):
        await run_node(
            FileNode(), {"path": "a.txt", "operation": "write", "content": "one"}
        )
        await run_node(
            FileNode(), {"path": "a.txt", "operation": "append", "content": "-two"}
        )
        result = await run_node(FileNode(), {"path": "a.txt", "operation": "read"})
        assert result["content"] == "one-two"

    async def test_exists_and_delete(self, tmp_media_root):
        await run_node(
            FileNode(), {"path": "gone.txt", "operation": "write", "content": "x"}
        )
        assert (await run_node(
            FileNode(), {"path": "gone.txt", "operation": "exists"}
        ))["exists"] is True

        await run_node(FileNode(), {"path": "gone.txt", "operation": "delete"})
        assert (await run_node(
            FileNode(), {"path": "gone.txt", "operation": "exists"}
        ))["exists"] is False

    async def test_path_traversal_blocked(self, tmp_media_root):
        with pytest.raises(SecurityError):
            await run_node(
                FileNode(), {"path": "../../etc/passwd", "operation": "read"}
            )

    async def test_absolute_path_blocked(self, tmp_media_root):
        with pytest.raises(SecurityError):
            await run_node(FileNode(), {"path": "/etc/passwd", "operation": "read"})

    async def test_missing_file_is_not_found(self, tmp_media_root):
        with pytest.raises((NodeExecutionError, ValidationError)):
            await run_node(FileNode(), {"path": "nope.txt", "operation": "read"})

    async def test_requires_a_path(self, tmp_media_root):
        with pytest.raises(ValidationError):
            await run_node(FileNode(), {"path": "", "operation": "read"})

    async def test_write_respects_max_bytes(self, tmp_media_root):
        with pytest.raises(ValidationError):
            await run_node(
                FileNode(),
                {
                    "path": "big.txt",
                    "operation": "write",
                    "content": "x" * 100,
                    "max_bytes": 10,
                },
            )

    async def test_folder_create_and_list(self, tmp_media_root):
        await run_node(FolderNode(), {"path": "sub", "operation": "create"})
        await run_node(
            FileNode(), {"path": "sub/f.txt", "operation": "write", "content": "x"}
        )
        result = await run_node(FolderNode(), {"path": "sub", "operation": "list"})
        assert result["count"] == 1
        assert result["entries"][0]["name"] == "f.txt"

    async def test_folder_traversal_blocked(self, tmp_media_root):
        with pytest.raises(SecurityError):
            await run_node(FolderNode(), {"path": "../..", "operation": "list"})

    async def test_folder_refuses_to_delete_root(self, tmp_media_root):
        with pytest.raises(SecurityError):
            await run_node(FolderNode(), {"path": "", "operation": "delete"})

    async def test_folder_missing_directory(self, tmp_media_root):
        with pytest.raises(NodeExecutionError):
            await run_node(FolderNode(), {"path": "absent", "operation": "list"})


# --------------------------------------------------------------------------- #
# AI / media nodes
# --------------------------------------------------------------------------- #
class TestAIAndMediaNodes:
    async def test_prompt_renders_template(self):
        from app.services.workflow.nodes.ai_nodes import PromptNode

        context = NodeContext(variables={"topic": "otters"})
        result = await run_node(
            PromptNode(), {"template": "Write about {{ vars.topic }}"}, context
        )
        assert result["prompt"] == "Write about otters"
        assert result["estimated_tokens"] > 0

    async def test_prompt_requires_template(self):
        from app.services.workflow.nodes.ai_nodes import PromptNode

        with pytest.raises(ValidationError):
            await run_node(PromptNode(), {"template": "   "})

    async def test_image_node_without_provider_fails_clearly(self):
        from app.services.ai.orchestrator import ai_orchestrator
        from app.services.workflow.nodes.ai_nodes import ImageGenerationNode

        ai_orchestrator.clear_optional_providers()
        with pytest.raises(NodeExecutionError) as excinfo:
            await run_node(ImageGenerationNode(), {"prompt": "a cat"})
        assert excinfo.value.code == NodeErrorCode.PROVIDER

    async def test_tts_without_provider_fails_clearly(self):
        from app.services.ai.orchestrator import ai_orchestrator

        ai_orchestrator.clear_optional_providers()
        with pytest.raises(NodeExecutionError) as excinfo:
            await run_node(TTSNode(), {"text": "hello"})
        assert excinfo.value.code == NodeErrorCode.PROVIDER

    async def test_tts_requires_text(self):
        with pytest.raises(ValidationError):
            await run_node(TTSNode(), {"text": "  "})

    async def test_tts_uses_registered_provider(self, tmp_media_root):
        from app.services.ai.orchestrator import ai_orchestrator

        class StubTTS:
            name = "stub"

            async def synthesize(self, **kwargs):
                return {"audio_path": "out.wav", "duration_seconds": 1.0}

        ai_orchestrator.clear_optional_providers()
        ai_orchestrator.register_speech_provider("tts", "stub", StubTTS())
        try:
            result = await run_node(TTSNode(), {"text": "hello"})
            assert result["audio_path"] == "out.wav"
        finally:
            ai_orchestrator.clear_optional_providers()

    async def test_stt_requires_existing_audio(self, tmp_media_root):
        with pytest.raises(ValidationError):
            await run_node(STTNode(), {"audio_path": "missing.wav"})

    async def test_stt_without_provider_fails_clearly(self, tmp_media_root):
        from app.services.ai.orchestrator import ai_orchestrator

        (tmp_media_root / "clip.wav").write_bytes(b"RIFF")
        ai_orchestrator.clear_optional_providers()
        with pytest.raises(NodeExecutionError) as excinfo:
            await run_node(STTNode(), {"audio_path": "clip.wav"})
        assert excinfo.value.code == NodeErrorCode.PROVIDER

    def test_ffmpeg_rejects_path_like_extra_args(self):
        node = FFmpegNode()
        with pytest.raises(SecurityError):
            node._screen_extra_args("-vf ../../etc/passwd")

    def test_ffmpeg_rejects_blocked_flags(self):
        node = FFmpegNode()
        with pytest.raises(SecurityError):
            node._screen_extra_args("-i")

    def test_ffmpeg_allows_plain_flags(self):
        assert FFmpegNode()._screen_extra_args("-preset fast") == ["-preset", "fast"]

    def test_ffmpeg_builds_expected_command(self):
        cmd = FFmpegNode()._build_command(
            {"operation": "extract_audio", "audio_codec": "aac"}, "/in.mp4", "/out.m4a"
        )
        assert "-vn" in cmd and cmd[-1] == "/out.m4a"

    async def test_ffmpeg_requires_both_paths(self, tmp_media_root):
        with pytest.raises(ValidationError):
            await run_node(FFmpegNode(), {"input_path": "a.mp4", "output_path": ""})

    async def test_media_processing_requires_asset_id(self):
        with pytest.raises(ValidationError):
            await run_node(MediaProcessingNode(), {"operation": "process"})

    async def test_media_probe_requires_path(self):
        with pytest.raises(ValidationError):
            await run_node(MediaProcessingNode(), {"operation": "probe"})
