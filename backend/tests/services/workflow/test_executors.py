"""Tests for workflow node executors, including SSRF and shell hardening."""

import asyncio

import pytest

from app.core.errors import ExecutionError, SecurityError, ValidationError
from app.infrastructure.config.settings import settings
from app.services.workflow.executors import (
    BaseNodeExecutor,
    BranchExecutor,
    CommandExecutor,
    DelayExecutor,
    DummyNodeExecutor,
    ExecutorRegistry,
    HttpRequestExecutor,
    MathAddExecutor,
    MathExpressionExecutor,
    NoOpExecutor,
    TemplateExecutor,
    TransformExecutor,
    coerce_number,
    is_truthy,
    render_template,
    render_value,
    resolve_reference,
    validate_outbound_url,
)


class FakeNode:
    def __init__(self, name="node", node_type="dummy", config=None, node_id=1):
        self.id = node_id
        self.name = name
        self.node_type = node_type
        self.config = config or {}
        self.retry_policy = None


# --------------------------------------------------------------------------- #
# Context helpers
# --------------------------------------------------------------------------- #
class TestResolveReference:
    def test_exact_key(self):
        assert resolve_reference({"a": 1}, "a") == 1

    def test_integer_key_coercion(self):
        assert resolve_reference({3: {"result": 7}}, "3") == {"result": 7}

    def test_dotted_path(self):
        assert resolve_reference({3: {"result": 7}}, "3.result") == 7

    def test_nested_dict_path(self):
        ctx = {1: {"response": {"items": [{"id": "x"}]}}}
        assert resolve_reference(ctx, "1.response.items.0.id") == "x"

    def test_list_index(self):
        assert resolve_reference({1: {"vals": [10, 20]}}, "1.vals.1") == 20

    def test_missing_returns_none(self):
        assert resolve_reference({}, "nope.deep") is None

    def test_empty_expression(self):
        assert resolve_reference({"a": 1}, "  ") is None

    def test_index_out_of_range(self):
        assert resolve_reference({1: {"v": [1]}}, "1.v.9") is None

    def test_scalar_traversal_stops(self):
        assert resolve_reference({1: 5}, "1.deeper") is None


class TestRenderTemplate:
    def test_simple_substitution(self):
        assert render_template("Hi {{ 1.name }}", {1: {"name": "Ada"}}) == "Hi Ada"

    def test_missing_renders_empty(self):
        assert render_template("x={{ nope }}", {}) == "x="

    def test_no_placeholders_passthrough(self):
        assert render_template("plain text", {}) == "plain text"

    def test_object_serialised_as_json(self):
        out = render_template("{{ 1.data }}", {1: {"data": {"k": 1}}})
        assert out == '{"k": 1}'

    def test_multiple_placeholders(self):
        ctx = {1: {"a": "x"}, 2: {"b": "y"}}
        assert render_template("{{1.a}}-{{2.b}}", ctx) == "x-y"


class TestRenderValue:
    def test_recurses_into_dict(self):
        out = render_value({"k": "{{ 1.v }}"}, {1: {"v": "z"}})
        assert out == {"k": "z"}

    def test_recurses_into_list(self):
        assert render_value(["{{ 1.v }}"], {1: {"v": "z"}}) == ["z"]

    def test_passes_through_non_strings(self):
        assert render_value(42, {}) == 42


class TestCoercionHelpers:
    @pytest.mark.parametrize(
        "value,expected",
        [(1, 1.0), (1.5, 1.5), ("2.5", 2.5), (True, 1.0), ({"result": 3}, 3.0)],
    )
    def test_coerce_number(self, value, expected):
        assert coerce_number(value) == expected

    def test_coerce_number_default_on_garbage(self):
        assert coerce_number("abc", 9.0) == 9.0
        assert coerce_number(None, 4.0) == 4.0

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("false", False), ("0", False), ("", False), ("no", False),
            ("yes", True), ([], False), ([1], True), ({}, False), (0, False), (5, True),
        ],
    )
    def test_is_truthy(self, value, expected):
        assert is_truthy(value) is expected


# --------------------------------------------------------------------------- #
# SSRF protection
# --------------------------------------------------------------------------- #
class TestValidateOutboundUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://example.com/x",
            "gopher://example.com",
            "data:text/plain,hi",
        ],
    )
    def test_rejects_non_http_schemes(self, url):
        with pytest.raises(SecurityError):
            validate_outbound_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8000/admin",
            "http://localhost:8000/admin",
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://172.16.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "http://0.0.0.0/",
        ],
    )
    def test_blocks_private_and_loopback(self, url):
        with pytest.raises(SecurityError):
            validate_outbound_url(url, allow_private=False)

    def test_blocks_cloud_metadata_hostname(self):
        with pytest.raises(SecurityError):
            validate_outbound_url("http://metadata.google.internal/", allow_private=False)

    def test_allows_private_when_explicitly_enabled(self):
        assert validate_outbound_url("http://127.0.0.1:9/", allow_private=True)

    def test_rejects_empty_url(self):
        with pytest.raises(ValidationError):
            validate_outbound_url("")

    def test_rejects_missing_hostname(self):
        with pytest.raises(ValidationError):
            validate_outbound_url("http://")

    def test_allowlist_permits_listed_host(self):
        assert validate_outbound_url(
            "http://127.0.0.1/x", allowed_hosts=["127.0.0.1"], allow_private=False
        )

    def test_allowlist_blocks_unlisted_host(self):
        with pytest.raises(SecurityError):
            validate_outbound_url(
                "https://evil.example/x", allowed_hosts=["good.example"]
            )

    def test_unresolvable_host_raises_validation(self):
        with pytest.raises((ValidationError, SecurityError)):
            validate_outbound_url("http://this-host-should-not-exist-000.invalid/")


# --------------------------------------------------------------------------- #
# Executors
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestSimpleExecutors:
    async def test_noop(self):
        result = await NoOpExecutor().execute(FakeNode(name="n"), {})
        assert result == {"status": "ok", "node": "n"}

    async def test_dummy_backward_compatible(self):
        result = await DummyNodeExecutor().execute(FakeNode(name="d"), {})
        assert result["status"] == "ok"

    async def test_delay_sleeps(self):
        node = FakeNode(config={"seconds": 0.01})
        result = await DelayExecutor().execute(node, {})
        assert result["slept_seconds"] == pytest.approx(0.01)

    async def test_delay_clamps_negative(self):
        result = await DelayExecutor().execute(FakeNode(config={"seconds": -5}), {})
        assert result["slept_seconds"] == 0.0

    async def test_delay_clamps_to_max(self):
        node = FakeNode(config={"seconds": 999999})
        # Patch sleep so the test does not actually wait an hour.
        original = asyncio.sleep
        recorded = {}

        async def fake_sleep(sec):
            recorded["sec"] = sec
            await original(0)

        asyncio.sleep = fake_sleep
        try:
            result = await DelayExecutor().execute(node, {})
        finally:
            asyncio.sleep = original
        assert recorded["sec"] == 3600.0
        assert result["slept_seconds"] == 3600.0


@pytest.mark.asyncio
class TestMathExecutors:
    async def test_add_literals(self):
        node = FakeNode(config={"a": 10, "b": 5})
        assert (await MathAddExecutor().execute(node, {}))["result"] == 15

    async def test_add_resolves_node_reference(self):
        """V1.0 semantics: an int operand may reference an upstream node id."""
        node = FakeNode(config={"a": 1, "b": 2})
        context = {1: {"result": 100}, 2: {"result": 5}}
        assert (await MathAddExecutor().execute(node, context))["result"] == 105

    async def test_add_falls_back_to_literal_when_no_reference(self):
        node = FakeNode(config={"a": 7, "b": 3})
        assert (await MathAddExecutor().execute(node, {}))["result"] == 10

    async def test_add_template_operand(self):
        # String operands are templated; ints keep V1.0 node-reference semantics,
        # so 'b' is given as a string to stay a literal.
        node = FakeNode(config={"a": "{{ 1.result }}", "b": "1"})
        assert (await MathAddExecutor().execute(node, {1: {"result": 41}}))["result"] == 42

    async def test_int_operand_still_resolves_node_reference(self):
        """Regression guard for V1.0 behaviour: int operands index the context."""
        node = FakeNode(config={"a": "{{ 1.result }}", "b": 1})
        # 'b': 1 resolves to node 1's result (41), so 41 + 41 == 82.
        assert (await MathAddExecutor().execute(node, {1: {"result": 41}}))["result"] == 82

    async def test_add_defaults_missing_operands_to_zero(self):
        assert (await MathAddExecutor().execute(FakeNode(config={}), {}))["result"] == 0

    async def test_add_produces_float_when_needed(self):
        node = FakeNode(config={"a": 1.5, "b": 1.25})
        assert (await MathAddExecutor().execute(node, {}))["result"] == 2.75

    async def test_expression_evaluates(self):
        node = FakeNode(config={"expression": "2 + 3 * 4"})
        assert (await MathExpressionExecutor().execute(node, {}))["result"] == 14

    async def test_expression_with_template(self):
        node = FakeNode(config={"expression": "{{ 1.result }} * 2"})
        result = await MathExpressionExecutor().execute(node, {1: {"result": 21}})
        assert result["result"] == 42

    async def test_expression_rejects_code_injection(self):
        node = FakeNode(config={"expression": "__import__('os').system('ls')"})
        with pytest.raises(SecurityError):
            await MathExpressionExecutor().execute(node, {})

    async def test_expression_rejects_letters(self):
        with pytest.raises(SecurityError):
            await MathExpressionExecutor().execute(
                FakeNode(config={"expression": "open('x')"}), {}
            )

    async def test_expression_requires_value(self):
        with pytest.raises(ValidationError):
            await MathExpressionExecutor().execute(FakeNode(config={}), {})

    async def test_expression_rejects_overlong_input(self):
        node = FakeNode(config={"expression": "1+" * 300 + "1"})
        with pytest.raises(ValidationError):
            await MathExpressionExecutor().execute(node, {})

    async def test_expression_division_by_zero_is_execution_error(self):
        with pytest.raises(ExecutionError):
            await MathExpressionExecutor().execute(
                FakeNode(config={"expression": "1/0"}), {}
            )


@pytest.mark.asyncio
class TestTransformExecutors:
    async def test_template_renders(self):
        node = FakeNode(config={"template": "Hello {{ 1.name }}"})
        result = await TemplateExecutor().execute(node, {1: {"name": "World"}})
        assert result["result"] == "Hello World"

    async def test_template_requires_string(self):
        with pytest.raises(ValidationError):
            await TemplateExecutor().execute(FakeNode(config={"template": 5}), {})

    async def test_transform_maps_fields(self):
        node = FakeNode(config={"fields": {"greeting": "hi {{ 1.n }}", "static": 3}})
        result = await TransformExecutor().execute(node, {1: {"n": "bob"}})
        assert result["result"] == {"greeting": "hi bob", "static": 3}

    async def test_transform_requires_object(self):
        with pytest.raises(ValidationError):
            await TransformExecutor().execute(FakeNode(config={"fields": "x"}), {})


@pytest.mark.asyncio
class TestBranchExecutor:
    @pytest.mark.parametrize(
        "operator,left,right,expected",
        [
            ("==", "a", "a", True),
            ("==", "a", "b", False),
            ("!=", "a", "b", True),
            (">", 5, 3, True),
            (">=", 3, 3, True),
            ("<", 1, 2, True),
            ("<=", 3, 2, False),
            ("contains", "hello world", "world", True),
            ("contains", "hello", "zzz", False),
        ],
    )
    async def test_operators(self, operator, left, right, expected):
        node = FakeNode(config={"operator": operator, "left": left, "right": right})
        result = await BranchExecutor().execute(node, {})
        assert result["result"] is expected
        assert result["branch"] == ("true" if expected else "false")

    async def test_truthy_default(self):
        node = FakeNode(config={"left": "yes"})
        assert (await BranchExecutor().execute(node, {}))["result"] is True

    async def test_unknown_operator_raises(self):
        node = FakeNode(config={"operator": "~=", "left": 1, "right": 1})
        with pytest.raises(ValidationError):
            await BranchExecutor().execute(node, {})

    async def test_reads_upstream_value(self):
        node = FakeNode(config={"operator": "==", "left": "{{ 1.status }}", "right": "ok"})
        result = await BranchExecutor().execute(node, {1: {"status": "ok"}})
        assert result["result"] is True


@pytest.mark.asyncio
class TestHttpRequestExecutor:
    async def test_rejects_unknown_method(self):
        node = FakeNode(config={"url": "https://example.com", "method": "TRACE"})
        with pytest.raises(ValidationError):
            await HttpRequestExecutor().execute(node, {})

    async def test_blocks_ssrf_target(self):
        node = FakeNode(config={"url": "http://169.254.169.254/latest/meta-data/"})
        with pytest.raises(SecurityError):
            await HttpRequestExecutor().execute(node, {})

    async def test_requires_url(self):
        with pytest.raises(ValidationError):
            await HttpRequestExecutor().execute(FakeNode(config={}), {})

    async def test_rejects_non_object_headers(self):
        node = FakeNode(
            config={"url": "http://127.0.0.1/x", "headers": "nope"}
        )
        with pytest.raises((ValidationError, SecurityError)):
            await HttpRequestExecutor().execute(node, {})

    async def test_successful_request(self, monkeypatch):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)
        original_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = transport
            return original_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)
        monkeypatch.setattr(
            "app.services.workflow.executors.validate_outbound_url", lambda u, **k: u
        )

        node = FakeNode(config={"url": "https://api.example.com/data"})
        result = await HttpRequestExecutor().execute(node, {})
        assert result["status_code"] == 200
        assert result["ok"] is True
        assert result["response"] == {"ok": True}

    async def test_timeout_becomes_execution_error(self, monkeypatch):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out", request=request)

        transport = httpx.MockTransport(handler)
        original_client = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda *a, **kw: original_client(*a, **{**kw, "transport": transport}),
        )
        monkeypatch.setattr(
            "app.services.workflow.executors.validate_outbound_url", lambda u, **k: u
        )
        node = FakeNode(config={"url": "https://api.example.com/slow"})
        with pytest.raises(ExecutionError, match="timed out"):
            await HttpRequestExecutor().execute(node, {})

    async def test_response_is_truncated(self, monkeypatch):
        import httpx

        big = b"x" * 5000
        transport = httpx.MockTransport(lambda r: httpx.Response(200, content=big))
        original_client = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda *a, **kw: original_client(*a, **{**kw, "transport": transport}),
        )
        monkeypatch.setattr(
            "app.services.workflow.executors.validate_outbound_url", lambda u, **k: u
        )
        monkeypatch.setattr(settings, "HTTP_EXECUTOR_MAX_RESPONSE_BYTES", 100)

        node = FakeNode(config={"url": "https://api.example.com/big"})
        result = await HttpRequestExecutor().execute(node, {})
        assert result["truncated"] is True
        assert len(result["response"]) == 100


@pytest.mark.asyncio
class TestCommandExecutor:
    async def test_disabled_by_default(self, monkeypatch):
        monkeypatch.setattr(settings, "ALLOW_SHELL_EXECUTOR", False)
        node = FakeNode(config={"command": "echo hi"})
        with pytest.raises(SecurityError, match="disabled"):
            await CommandExecutor().execute(node, {})

    async def test_empty_allowlist_blocks_everything(self, monkeypatch):
        monkeypatch.setattr(settings, "ALLOW_SHELL_EXECUTOR", True)
        monkeypatch.setattr(settings, "SHELL_ALLOWED_COMMANDS", [])
        node = FakeNode(config={"command": "echo hi"})
        with pytest.raises(SecurityError, match="allowlist|SHELL_ALLOWED"):
            await CommandExecutor().execute(node, {})

    async def test_non_allowlisted_command_blocked(self, monkeypatch):
        monkeypatch.setattr(settings, "ALLOW_SHELL_EXECUTOR", True)
        monkeypatch.setattr(settings, "SHELL_ALLOWED_COMMANDS", ["echo"])
        node = FakeNode(config={"command": "rm -rf /"})
        with pytest.raises(SecurityError, match="not in the allowlist"):
            await CommandExecutor().execute(node, {})

    async def test_shell_metacharacters_cannot_chain_commands(self, monkeypatch):
        """No shell is used, so ';' is just an argument to echo."""
        monkeypatch.setattr(settings, "ALLOW_SHELL_EXECUTOR", True)
        monkeypatch.setattr(settings, "SHELL_ALLOWED_COMMANDS", ["echo"])
        node = FakeNode(config={"command": "echo safe; touch /tmp/pwned_by_creator_os"})
        result = await CommandExecutor().execute(node, {})
        assert result["exit_code"] == 0
        assert "safe;" in result["stdout"]
        import os

        assert not os.path.exists("/tmp/pwned_by_creator_os")

    async def test_allowlisted_command_runs(self, monkeypatch):
        monkeypatch.setattr(settings, "ALLOW_SHELL_EXECUTOR", True)
        monkeypatch.setattr(settings, "SHELL_ALLOWED_COMMANDS", ["echo"])
        node = FakeNode(config={"command": "echo hello"})
        result = await CommandExecutor().execute(node, {})
        assert result["exit_code"] == 0
        assert result["stdout"] == "hello"

    async def test_requires_command(self, monkeypatch):
        monkeypatch.setattr(settings, "ALLOW_SHELL_EXECUTOR", True)
        monkeypatch.setattr(settings, "SHELL_ALLOWED_COMMANDS", ["echo"])
        with pytest.raises(ValidationError):
            await CommandExecutor().execute(FakeNode(config={"command": "  "}), {})

    async def test_unparseable_command(self, monkeypatch):
        monkeypatch.setattr(settings, "ALLOW_SHELL_EXECUTOR", True)
        monkeypatch.setattr(settings, "SHELL_ALLOWED_COMMANDS", ["echo"])
        with pytest.raises(ValidationError):
            await CommandExecutor().execute(FakeNode(config={"command": 'echo "x'}), {})

    async def test_timeout_kills_process(self, monkeypatch):
        monkeypatch.setattr(settings, "ALLOW_SHELL_EXECUTOR", True)
        monkeypatch.setattr(settings, "SHELL_ALLOWED_COMMANDS", ["sleep"])
        node = FakeNode(config={"command": "sleep 30", "timeout": 1})
        with pytest.raises(ExecutionError, match="timed out"):
            await CommandExecutor().execute(node, {})


class TestExecutorRegistry:
    def test_v1_types_still_registered(self):
        registry = ExecutorRegistry()
        for node_type in ("dummy", "math_add", "http_request", "shell_command"):
            assert registry.has(node_type)

    def test_v11_types_registered(self):
        registry = ExecutorRegistry()
        for node_type in ("noop", "delay", "template", "transform", "branch", "math_expression"):
            assert registry.has(node_type)

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="No executor found"):
            ExecutorRegistry().get_executor("does_not_exist")

    def test_register_custom(self):
        class Custom(BaseNodeExecutor):
            label = "Custom"

            async def execute(self, node, context):
                return {"ok": True}

        registry = ExecutorRegistry()
        registry.register("custom", Custom())
        assert registry.has("custom")

    def test_register_duplicate_rejected(self):
        class Custom(BaseNodeExecutor):
            async def execute(self, node, context):
                return {}

        registry = ExecutorRegistry()
        registry.register("custom", Custom())
        with pytest.raises(ValidationError):
            registry.register("custom", Custom())

    def test_register_duplicate_with_override(self):
        class Custom(BaseNodeExecutor):
            async def execute(self, node, context):
                return {}

        registry = ExecutorRegistry()
        registry.register("custom", Custom())
        registry.register("custom", Custom(), override=True)
        assert registry.has("custom")

    def test_register_rejects_non_executor(self):
        with pytest.raises(ValidationError):
            ExecutorRegistry().register("bad", object())

    def test_unregister(self):
        registry = ExecutorRegistry()
        assert registry.unregister("dummy") is True
        assert registry.has("dummy") is False
        assert registry.unregister("dummy") is False

    def test_catalog_shape(self):
        catalog = ExecutorRegistry().catalog()
        assert len(catalog) >= 10
        entry = catalog[0]
        assert {"type", "label", "category", "description", "config_schema"} <= set(entry)

    def test_config_of_handles_none(self):
        node = FakeNode()
        node.config = None
        assert BaseNodeExecutor.config_of(node) == {}
