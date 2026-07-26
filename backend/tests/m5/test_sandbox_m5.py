"""M5: script sandbox containment and Python-node integration.

These tests encode the *verified* containment properties. Where the sandbox
does not fully contain something (see ``TestDocumentedLimitations``) that is
asserted too, so the honest limitation cannot quietly drift into a false claim
of safety.
"""

from __future__ import annotations

import pytest

from app.core.errors import SecurityError
from app.services.security.sandbox import (
    SandboxLimits,
    run_python_sandboxed,
    sandbox_available,
    sandbox_status,
)

pytestmark = pytest.mark.skipif(
    not sandbox_available(),
    reason="POSIX resource limits are unavailable on this platform",
)

ALLOWED = ("json", "math", "re", "datetime", "random", "hashlib", "base64")


@pytest.fixture
def limits():
    return SandboxLimits(
        cpu_seconds=3,
        memory_mb=192,
        wall_timeout_seconds=15.0,
        allowed_modules=ALLOWED,
        max_file_bytes=4096,
    )


class TestLegitimateScripts:
    def test_simple_expression_returns_a_result(self, limits):
        outcome = run_python_sandboxed("result = sum([1, 2, 3])", {}, limits)
        assert outcome.ok is True
        assert outcome.result == 6

    def test_inputs_are_bound_as_locals(self, limits):
        outcome = run_python_sandboxed("result = x * 2", {"x": 21}, limits)
        assert outcome.result == 42

    def test_stdout_is_captured(self, limits):
        outcome = run_python_sandboxed("print('hello')\nresult = 1", {}, limits)
        assert "hello" in outcome.stdout

    def test_public_variables_are_returned(self, limits):
        outcome = run_python_sandboxed("total = 5\nresult = total", {}, limits)
        assert outcome.variables.get("total") == 5

    @pytest.mark.parametrize(
        "module,snippet",
        [
            ("json", "import json\nresult = json.dumps({'a': 1})"),
            ("math", "import math\nresult = math.sqrt(9)"),
            ("re", "import re\nresult = bool(re.match('a', 'ab'))"),
            ("datetime", "import datetime\nresult = str(datetime.date(2020, 1, 1))"),
            ("random", "import random\nrandom.seed(1)\nresult = type(random.random()).__name__"),
            ("hashlib", "import hashlib\nresult = hashlib.sha256(b'x').hexdigest()[:8]"),
            ("base64", "import base64\nresult = base64.b64encode(b'hi').decode()"),
        ],
    )
    def test_allowlisted_modules_are_usable(self, limits, module, snippet):
        """Regression guard: warm-up ordering bugs silently broke these."""
        outcome = run_python_sandboxed(snippet, {}, limits)
        assert outcome.ok is True, f"{module} failed: {outcome.error}"

    def test_syntax_error_is_reported_cleanly(self, limits):
        outcome = run_python_sandboxed("def (:", {}, limits)
        assert outcome.ok is False
        assert outcome.error_type == "SyntaxError"

    def test_runtime_error_is_reported_with_its_type(self, limits):
        outcome = run_python_sandboxed("result = 1 / 0", {}, limits)
        assert outcome.ok is False
        assert outcome.error_type == "ZeroDivisionError"


class TestResourceLimits:
    def test_infinite_loop_is_killed_by_the_cpu_limit(self, limits):
        """The headline M4 defect: a busy loop pinned a core indefinitely."""
        outcome = run_python_sandboxed("while True:\n    pass", {}, limits)
        assert outcome.ok is False
        assert outcome.killed_by_limit is True

    def test_memory_bomb_is_contained(self, limits):
        """Previously OOM-killed the entire backend process."""
        outcome = run_python_sandboxed("x = [0] * 10**9", {}, limits)
        assert outcome.ok is False
        assert outcome.killed_by_limit is True
        assert outcome.error  # never a blank message

    def test_sleeping_script_hits_the_wall_clock_timeout(self):
        """RLIMIT_CPU counts CPU time only, so a sleeper needs the wall clock."""
        limits = SandboxLimits(
            cpu_seconds=30,
            memory_mb=128,
            wall_timeout_seconds=2.0,
            allowed_modules=("time",),
        )
        outcome = run_python_sandboxed("import time\ntime.sleep(30)", {}, limits)
        assert outcome.ok is False
        assert outcome.timed_out is True

    def test_failures_always_carry_an_explanation(self, limits):
        for code in ("while True:\n    pass", "x = [0] * 10**9", "import os"):
            outcome = run_python_sandboxed(code, {}, limits)
            assert outcome.ok is False
            assert outcome.error, f"blank error for {code!r}"


class TestContainment:
    @pytest.mark.parametrize(
        "code",
        [
            "import os",
            "import sys",
            "import subprocess",
            "import socket",
            "import ctypes",
            "import shutil",
            "import pathlib",
        ],
    )
    def test_denied_modules_cannot_be_imported(self, limits, code):
        outcome = run_python_sandboxed(code, {}, limits)
        assert outcome.ok is False

    def test_open_is_removed_from_builtins(self, limits):
        outcome = run_python_sandboxed("result = open('/etc/hostname').read()", {}, limits)
        assert outcome.ok is False

    def test_nested_exec_and_compile_are_refused(self, limits):
        for code in ("exec('y = 1')", "compile('1', '<s>', 'eval')"):
            outcome = run_python_sandboxed(code, {}, limits)
            assert outcome.ok is False
            assert "Dynamic code execution" in (outcome.error or "")

    def test_environment_secrets_are_not_visible(self, limits, monkeypatch):
        """A leaked API key in the parent env must not reach the child."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-be-visible")
        monkeypatch.setenv("DATABASE_URL", "postgres://secret@host/db")
        escape = (
            "def esc(n):\n"
            "    for c in ().__class__.__bases__[0].__subclasses__():\n"
            "        if c.__name__ == 'BuiltinImporter':\n"
            "            return c.load_module(n)\n"
            "m = esc('posix')\n"
            "result = dict(m.environ)\n"
        )
        outcome = run_python_sandboxed(escape, {}, limits)
        serialised = str(outcome.result) + str(outcome.variables)
        assert "sk-should-never-be-visible" not in serialised
        assert "postgres://secret" not in serialised


class TestPostEscapeContainment:
    """The __subclasses__ escape yields a module reference; it must stay inert.

    ``BuiltinImporter.load_module`` bypasses the import allowlist -- verified,
    and documented in docs/SECURITY.md. The PEP 578 audit hook is what makes
    the recovered reference useless, and that is what these tests pin down.
    """

    ESCAPE = (
        "def esc(n):\n"
        "    for c in ().__class__.__bases__[0].__subclasses__():\n"
        "        if c.__name__ == 'BuiltinImporter':\n"
        "            return c.load_module(n)\n"
        "m = esc('posix')\n"
    )

    @pytest.mark.parametrize(
        "operation",
        [
            "fd = m.open('/etc/passwd', 0)",
            "m.system('id')",
            "m.fork()",
            "m.remove('/tmp/creator-os-should-not-exist')",
            "m.chmod('/tmp', 0o777)",
            "m.kill(1, 9)",
            "m.listdir('/')",
            "m.scandir('/')",
            "m.chdir('/')",
        ],
    )
    def test_dangerous_operations_are_refused(self, limits, operation):
        outcome = run_python_sandboxed(self.ESCAPE + operation, {}, limits)
        assert outcome.ok is False, f"{operation} was permitted"

    def test_resource_limits_still_apply_after_an_escape(self, limits):
        outcome = run_python_sandboxed(self.ESCAPE + "while True:\n    pass", {}, limits)
        assert outcome.ok is False
        assert outcome.killed_by_limit is True


class TestDocumentedLimitations:
    """Assert the boundaries we do NOT claim, so the docs stay truthful."""

    def test_sandbox_does_not_claim_to_be_a_security_boundary(self):
        status = sandbox_status()
        assert status["is_security_boundary"] is False
        assert "not a security boundary" in status["notes"].lower()

    def test_module_reference_is_still_obtainable_via_subclasses(self, limits):
        """Documented in docs/SECURITY.md as a known, mitigated weakness.

        If a future CPython or a fix ever closes this, the assertion below
        will fail and the documentation must be updated to match.
        """
        escape = (
            "def esc(n):\n"
            "    for c in ().__class__.__bases__[0].__subclasses__():\n"
            "        if c.__name__ == 'BuiltinImporter':\n"
            "            return c.load_module(n)\n"
            "result = esc('posix') is not None\n"
        )
        outcome = run_python_sandboxed(escape, {}, limits)
        assert outcome.ok is True and outcome.result is True


class TestPythonNodeIntegration:
    @pytest.fixture
    def python_node(self, monkeypatch):
        from app.infrastructure.config.settings import settings
        from app.services.workflow.nodes.data_nodes import PythonNode

        monkeypatch.setattr(settings, "ALLOW_PYTHON_EXECUTOR", True)
        monkeypatch.setattr(settings, "SCRIPT_SANDBOX_ENABLED", True)
        monkeypatch.setattr(settings, "SCRIPT_SANDBOX_CPU_SECONDS", 3)
        monkeypatch.setattr(settings, "SCRIPT_SANDBOX_MEMORY_MB", 192)
        monkeypatch.setattr(settings, "PYTHON_EXECUTOR_TIMEOUT_SECONDS", 15.0)
        return PythonNode()

    async def _run(self, node, config, context=None):
        class _Node:
            def __init__(self, cfg):
                self.config = cfg

        return await node.run(_Node(config), context if context is not None else {}, config)

    async def test_node_executes_through_the_sandbox(self, python_node):
        outcome = await self._run(python_node, {"code": "result = 6 * 7"})
        assert outcome["result"] == 42

    async def test_node_binds_inputs(self, python_node):
        outcome = await self._run(
            python_node, {"code": "result = x + 1", "inputs": {"x": 41}}
        )
        assert outcome["result"] == 42

    async def test_node_is_disabled_by_default(self, monkeypatch):
        from app.infrastructure.config.settings import settings
        from app.services.workflow.nodes.data_nodes import PythonNode

        monkeypatch.setattr(settings, "ALLOW_PYTHON_EXECUTOR", False)
        assert PythonNode().is_enabled() is False

    async def test_memory_limit_surfaces_as_a_node_error(self, python_node):
        from app.services.workflow.runtime import NodeExecutionError

        with pytest.raises(NodeExecutionError):
            await self._run(python_node, {"code": "x = [0] * 10**9"})

    async def test_blocked_import_is_a_security_error(self, python_node):
        """Policy violations must not be retried by the engine."""
        with pytest.raises(SecurityError):
            await self._run(python_node, {"code": "import os\nresult = 1"})

    async def test_execution_quota_is_enforced_per_run(self, python_node, monkeypatch):
        """A loop node wrapping a script node cannot spawn unbounded processes."""
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "SCRIPT_EXECUTION_QUOTA_PER_RUN", 3)
        context: dict = {}
        for _ in range(3):
            await self._run(python_node, {"code": "result = 1"}, context)
        with pytest.raises(SecurityError) as excinfo:
            await self._run(python_node, {"code": "result = 1"}, context)
        assert "quota" in str(excinfo.value).lower()

    async def test_empty_code_is_a_validation_error(self, python_node):
        from app.core.errors import ValidationError

        with pytest.raises(ValidationError):
            await self._run(python_node, {"code": "   "})
