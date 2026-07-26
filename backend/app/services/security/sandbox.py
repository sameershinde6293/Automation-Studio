"""Process-isolated script execution with OS resource limits (M5).

Read this before trusting it
----------------------------
This is **defence in depth, not a security boundary**. It is a large
improvement over the M4 in-process ``exec``, and it is honest about what it
still cannot do. See ``docs/SECURITY.md`` for the full statement.

What M4 had, and why it was not enough
--------------------------------------
The Python node ran ``exec`` in the backend process with a stripped
``__builtins__`` and a regex blocklist. Three concrete failures:

1. A wall-clock ``asyncio.wait_for`` cannot cancel a running thread, so
   ``while True: pass`` pinned a CPU core for the process lifetime.
2. ``[0] * 10**10`` OOM-killed the entire backend.
3. Restricted-``exec`` escapes are a well-documented class of bug; a blocklist
   over source text is bypassable.

What this module adds
---------------------
Execution moves into a **separate OS process** that, before running any user
code, lowers its own limits via ``resource.setrlimit``:

* ``RLIMIT_CPU``    — hard CPU-seconds cap. The kernel kills a busy loop; this
  is the fix for (1), which no timeout in the parent can achieve.
* ``RLIMIT_AS``     — address-space cap. Allocation fails inside the child
  instead of taking down the backend. Fix for (2).
* ``RLIMIT_FSIZE``  — maximum bytes writable to any file.
* ``RLIMIT_NPROC``  — blocks fork bombs.
* ``RLIMIT_CORE``   — no core dumps (they could contain process memory).

Plus, in the child: a working directory in a private temp dir that is deleted
afterwards, a scrubbed environment (no ``OPENAI_API_KEY``, no ``DATABASE_URL``),
an import hook that allows only a configured module list, and socket
neutralisation to block network egress.

What it still does **not** guarantee
------------------------------------
* It is **not** a container, VM or seccomp jail. A CPython sandbox escape gives
  the attacker the backend user's own OS privileges.
* ``RLIMIT_AS`` is not enforced on all platforms; on Windows none of this
  applies and the module reports itself unavailable.
* Blocking ``socket`` stops ordinary network use, not a determined attacker
  with a syscall primitive.

Consequently the script nodes remain **disabled by default**. For untrusted
code, run the backend itself inside a container with seccomp and no network.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence

from app.infrastructure.logging.logger import get_logger

logger = get_logger("security.sandbox")

#: POSIX-only. ``resource`` does not exist on Windows.
try:  # pragma: no cover - platform dependent
    import resource  # type: ignore

    RESOURCE_LIMITS_AVAILABLE = True
except ImportError:  # pragma: no cover - Windows
    resource = None  # type: ignore
    RESOURCE_LIMITS_AVAILABLE = False


def sandbox_available() -> bool:
    """Whether OS-level resource limits can actually be applied here."""
    return RESOURCE_LIMITS_AVAILABLE and hasattr(os, "fork")


@dataclass
class SandboxLimits:
    """Resource ceilings applied inside the child process."""

    cpu_seconds: int = 10
    memory_mb: int = 256
    max_file_bytes: int = 1024 * 1024
    max_output_bytes: int = 256 * 1024
    #: Wall-clock timeout enforced by the parent, as a backstop to RLIMIT_CPU
    #: (which only counts CPU time, so a sleeping child would evade it).
    wall_timeout_seconds: float = 30.0
    max_processes: int = 0
    allowed_modules: Sequence[str] = ()
    block_network: bool = True

    @classmethod
    def from_settings(cls, settings: Any, *, wall_timeout: Optional[float] = None):
        return cls(
            cpu_seconds=int(settings.SCRIPT_SANDBOX_CPU_SECONDS),
            memory_mb=int(settings.SCRIPT_SANDBOX_MEMORY_MB),
            max_file_bytes=int(settings.SCRIPT_SANDBOX_MAX_FILE_BYTES),
            max_output_bytes=int(settings.SCRIPT_SANDBOX_MAX_OUTPUT_BYTES),
            wall_timeout_seconds=float(
                wall_timeout
                if wall_timeout is not None
                else settings.PYTHON_EXECUTOR_TIMEOUT_SECONDS
            ),
            allowed_modules=tuple(settings.SCRIPT_SANDBOX_ALLOWED_MODULES),
            block_network=bool(settings.SCRIPT_SANDBOX_BLOCK_NETWORK),
        )


@dataclass
class SandboxResult:
    """Outcome of one sandboxed run."""

    ok: bool
    result: Any = None
    #: JSON-serialisable public locals after the run.
    variables: Dict[str, Any] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None
    error_type: Optional[str] = None
    exit_code: int = 0
    timed_out: bool = False
    #: True when the kernel killed the child (CPU/memory limit hit).
    killed_by_limit: bool = False


# --------------------------------------------------------------------------- #
# Child-process bootstrap
# --------------------------------------------------------------------------- #
#: Executed by a fresh interpreter. Receives a JSON job on stdin and writes a
#: JSON result to stdout. Kept as source text (rather than an importable
#: module) so the child needs nothing from the application package on its path.
_CHILD_BOOTSTRAP = r'''
import builtins, io, json, sys, os
from contextlib import redirect_stdout, redirect_stderr

job = json.loads(sys.stdin.read())
allowed = set(job.get("allowed_modules") or [])
block_network = bool(job.get("block_network", True))

# --- Resource limits ------------------------------------------------------
# Applied here, at the top of the child, rather than via subprocess'
# preexec_fn. preexec_fn runs between fork() and exec() in a process that has
# other threads (the backend runs a worker pool, APScheduler and asyncio
# executors), where only async-signal-safe calls are legal -- taking any lock
# held by another thread at fork time would deadlock the child. Setting the
# limits after exec, before any user code is compiled or run, is equally
# effective and has no such hazard.
_limit_errors = []
try:
    import resource
    _cpu = int(job.get("cpu_seconds") or 10)
    # SIGXCPU at the soft limit, SIGKILL one second later. This is the only
    # mechanism that reliably stops `while True: pass` -- a wall-clock timeout
    # in the parent cannot interrupt a tight loop that never yields.
    resource.setrlimit(resource.RLIMIT_CPU, (_cpu, _cpu + 1))

    _mem = int(job.get("memory_bytes") or 268435456)
    try:
        resource.setrlimit(resource.RLIMIT_AS, (_mem, _mem))
    except (ValueError, OSError) as exc:
        _limit_errors.append("RLIMIT_AS: %s" % exc)

    _fsize = int(job.get("max_file_bytes") or 1048576)
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (_fsize, _fsize))
    except (ValueError, OSError) as exc:
        _limit_errors.append("RLIMIT_FSIZE: %s" % exc)

    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError) as exc:
        _limit_errors.append("RLIMIT_CORE: %s" % exc)

    _nproc = int(job.get("max_processes") or 0)
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (_nproc + 1, _nproc + 1))
    except (ValueError, OSError) as exc:
        _limit_errors.append("RLIMIT_NPROC: %s" % exc)
except ImportError:
    _limit_errors.append("resource module unavailable")

# --- Network egress -------------------------------------------------------
# Neutralise the socket layer before user code runs. This stops ordinary
# network use (requests, urllib, raw sockets); it is not proof against a
# syscall-level escape.
if block_network:
    try:
        import socket
        class _BlockedSocket:
            def __init__(self, *a, **k):
                raise PermissionError("Network access is disabled in the sandbox.")
        socket.socket = _BlockedSocket
        socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(
            PermissionError("Network access is disabled in the sandbox."))
    except Exception:
        pass

# --- Import allowlist -----------------------------------------------------
# An allowlist, not a blocklist: anything not explicitly permitted is denied,
# so a module we failed to think of is denied by default rather than allowed.
#
# Internal helper modules that allowlisted stdlib packages pull in implicitly.
# `import hashlib` really imports `_hashlib`, `_io`, `binascii` and friends, so
# denying these would make the allowlist useless in practice. None of them
# grant a capability the audit hook does not already police: file, process,
# network and code-execution operations each raise their own audit event and
# are refused regardless of which module invoked them.
_IMPLICIT_MODULES = frozenset({
    "_io", "io", "_abc", "abc", "_collections", "_collections_abc",
    "_functools", "_operator", "operator", "keyword", "reprlib", "types",
    "_weakref", "weakref", "_weakrefset", "copyreg", "encodings", "codecs",
    "_codecs", "errno", "_locale", "_bootlocale", "warnings", "_warnings",
    "sre_compile", "sre_parse", "sre_constants", "_sre", "enum",
    "_hashlib", "_blake2", "_sha3", "_sha512", "_md5", "_sha1", "_sha256",
    "binascii", "_random", "_bisect", "bisect", "_struct", "struct",
    "_datetime", "_strptime", "_string", "_json", "_decimal",
    "numbers", "_heapq", "heapq", "copy", "_socket",
    "_stat", "stat", "genericpath", "posixpath", "_thread",
    "threading", "traceback", "linecache", "tokenize", "token",
    "unicodedata", "_uuid", "_statistics", "_pydecimal", "contextlib",
    "_frozen_importlib", "_frozen_importlib_external", "_imp", "zipimport",
    "_compat_pickle", "_bz2", "_lzma", "zlib", "time", "_ast",
})

def _import_permitted(name):
    root = str(name).split(".")[0]
    return root in allowed or root in _IMPLICIT_MODULES

_real_import = builtins.__import__

def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if not _import_permitted(name):
        raise ImportError(
            "Import of %r is not permitted in the sandbox. Allowed: %s"
            % (name, ", ".join(sorted(allowed)) or "(none)")
        )
    return _real_import(name, globals, locals, fromlist, level)

# NOTE: the guard is defined here but deliberately NOT installed yet. Warm-up
# (below) must import the allowlisted modules with the real __import__, because
# a stdlib module's own internal imports (datetime -> math, uuid -> os) would
# otherwise be refused and the module would fail to load at all. The guard is
# installed immediately after warm-up, before any user code runs.

# --- Audit hook (PEP 578) -------------------------------------------------
# Replacing builtins.__import__ alone is NOT sufficient. The classic escape
#
#     ().__class__.__bases__[0].__subclasses__()  ->  BuiltinImporter
#     BuiltinImporter.load_module("posix")
#
# reaches the import machinery directly and never calls builtins.__import__.
# It was verified working against an earlier revision of this sandbox.
#
# An audit hook fires inside CPython itself, below any Python-level
# indirection, and -- critically -- cannot be uninstalled once set, even by
# code that has recovered the real builtins. Every route into the import
# system, file opening, subprocess creation and socket use raises the
# corresponding audit event, so this closes the __subclasses__ family of
# escapes rather than merely making them inconvenient.
_BLOCKED_EVENTS = (
    "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork",
    "os.forkpty", "os.putenv", "os.unsetenv", "os.kill", "os.killpg",
    "os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod",
    "os.chown", "os.link", "os.symlink", "os.truncate",
    # Filesystem reconnaissance. Not destructive on its own, but it lets a
    # script map the host, so it is denied along with everything else.
    "os.listdir", "os.scandir", "os.walk", "os.stat", "os.lstat",
    "os.chdir", "os.getxattr", "os.listxattr", "os.setxattr",
    "glob.glob", "pathlib.Path.glob", "os.add_dll_directory",
    "subprocess.Popen", "shutil.rmtree", "shutil.move", "shutil.copyfile",
    "ctypes.dlopen", "ctypes.dlsym", "ctypes.call_function",
    "ctypes.addressof", "ctypes.get_errno",
    "socket.__new__", "socket.bind", "socket.connect", "socket.getaddrinfo",
    "socket.sendto", "urllib.Request", "webbrowser.open",
    "pty.spawn", "sys._getframe", "sys.settrace", "sys.setprofile",
    "code.__init__", "pickle.find_class", "marshal.loads",
)

# Single-use allowance for the one intended `exec` of the compiled script.
_exec_budget = [True]


def _audit_hook(event, args):
    # Import: enforce the same allowlist, but at the C level where
    # BuiltinImporter.load_module and importlib both pass through.
    if event == "import":
        module = (args[0] or "") if args else ""
        root = str(module).split(".")[0]
        if root and not _import_permitted(module):
            raise PermissionError(
                "Import of %r is not permitted in the sandbox." % module
            )
        return
    if event in ("open", "io.open"):
        raise PermissionError("File access is not permitted in the sandbox.")
    if event in ("exec", "compile"):
        # The script body is compiled before the hook is armed, then run via a
        # single intended exec of that code object. `_exec_budget` lets exactly
        # that one call through; any further exec/compile is the script trying
        # to build new code at runtime, which is refused.
        if _exec_budget:
            _exec_budget.pop()
            return
        raise PermissionError("Dynamic code execution is not permitted.")
    if block_network and event.startswith(("socket.", "urllib.", "ftplib.", "http.")):
        raise PermissionError("Network access is disabled in the sandbox.")
    if event in _BLOCKED_EVENTS:
        raise PermissionError(
            "Operation %r is not permitted in the sandbox." % event
        )


code = job["code"]
bound = job.get("inputs") or {}
scope = dict(bound)
scope["result"] = None

out, err = io.StringIO(), io.StringIO()
payload = {"ok": True, "result": None, "stdout": "", "stderr": ""}

# Compile BEFORE arming the hook: compilation itself raises a `compile` audit
# event, and pre-importing the modules the script is allowed to use must also
# happen while imports are still permitted through the normal path.
try:
    _compiled = compile(code, "<sandboxed-script>", "exec")
except SyntaxError as exc:
    sys.__stdout__.write("\u0000SANDBOX\u0000" + json.dumps({
        "ok": False, "error": "Syntax error: %s" % exc,
        "error_type": "SyntaxError", "stdout": "", "stderr": "",
    }))
    sys.__stdout__.flush()
    raise SystemExit(0)

# Warm the allowlisted modules so the script's own imports resolve from
# sys.modules without needing to re-enter the import machinery.
#
# This must fully complete before the audit hook is armed. Importing a module
# reads its .py/.pyc from disk, and several stdlib modules defer part of that
# work: `uuid` imports `hashlib` lazily, `random` pulls `_sha512`, and
# `datetime` may reach for `_strptime`. If those land after the hook is
# installed they raise "File access is not permitted", which would make
# perfectly legitimate scripts fail. Touching a representative attribute
# forces the lazy paths to resolve now, while file access is still allowed.
_WARMUP_TOUCH = {
    "uuid": lambda m: m.uuid5(m.NAMESPACE_DNS, "warmup"),
    "random": lambda m: (m.random(), m.randint(0, 1), m.choice([1, 2])),
    "datetime": lambda m: m.datetime.now().isoformat(),
    "statistics": lambda m: (m.mean([1, 2]), m.median([1, 2, 3])),
    "hashlib": lambda m: m.sha256(b"warmup").hexdigest(),
    "decimal": lambda m: str(m.Decimal("1.1") + m.Decimal("2.2")),
    "base64": lambda m: m.b64encode(b"warmup"),
    "json": lambda m: m.loads(m.dumps({"warmup": 1})),
    "re": lambda m: m.compile("warmup").match("warmup"),
    "textwrap": lambda m: m.fill("warmup", width=4),
    "string": lambda m: m.capwords("warm up"),
}
for _mod in sorted(allowed):
    try:
        _imported = _real_import(_mod)
        _touch = _WARMUP_TOUCH.get(_mod)
        if _touch is not None:
            try:
                _touch(sys.modules.get(_mod, _imported))
            except Exception:
                pass
    except Exception:
        pass

# Note on what the import allowlist can and cannot do.
#
# `builtins.__import__` and the `import` audit event both cover the normal
# import statement. Neither covers `BuiltinImporter.load_module("posix")`
# reached via `().__class__.__bases__[0].__subclasses__()`, because that path
# returns an already-initialised module without re-entering import_name.
#
# Evicting sys.modules to close that hole was tried and rejected: it breaks
# ordinary stdlib imports (datetime, decimal and anything else with C
# accelerators) for no real gain, because holding a reference to `posix` is
# only useful if its dangerous operations can actually be called -- and those
# (open, exec, system, fork, socket, chmod, ...) each raise an audit event that
# the hook above refuses. The audit hook, not the import allowlist, is the
# enforcement boundary; the allowlist is a clear, early error message for
# honest scripts. This is stated plainly in docs/SECURITY.md.

# Warm-up is finished: from here nothing legitimate needs to load new code, so
# the import allowlist can be enforced on the user's own import statements.
builtins.__import__ = _guarded_import

# --- Remove the most dangerous builtins ----------------------------------
# Done *after* warm-up: the import system uses builtins.open to read .py source
# for pure-Python stdlib modules, so removing it earlier made `import datetime`
# (and uuid, random, statistics) fail with a confusing "file access denied".
for _name in ("open", "input", "breakpoint", "exit", "quit", "help"):
    if hasattr(builtins, _name):
        try:
            delattr(builtins, _name)
        except Exception:
            pass

# Arm the audit hook. From this point the restriction is enforced by CPython
# itself and cannot be undone by anything the script does.
try:
    sys.addaudithook(_audit_hook)
except Exception as exc:
    _limit_errors.append("audit hook: %s" % exc)

try:
    with redirect_stdout(out), redirect_stderr(err):
        exec(_compiled, scope, scope)
    value = scope.get("result")
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        value = repr(value)
    payload["result"] = value
    variables = {}
    for key, val in scope.items():
        if key.startswith("_"):
            continue
        if isinstance(val, (str, int, float, bool, list, dict, type(None))):
            try:
                json.dumps(val)
                variables[key] = val
            except (TypeError, ValueError):
                continue
    payload["variables"] = variables
except BaseException as exc:
    payload["ok"] = False
    # Several important exceptions carry an empty str(): MemoryError raised by
    # RLIMIT_AS is the common one here. Fall back to the type name so the
    # caller never receives a blank explanation for a failed run.
    _detail = str(exc)[:2000]
    if not _detail:
        _detail = {
            "MemoryError": "Script exceeded its memory limit.",
            "KeyboardInterrupt": "Script was interrupted.",
            "SystemExit": "Script called exit().",
        }.get(type(exc).__name__, type(exc).__name__)
    payload["error"] = _detail
    payload["error_type"] = type(exc).__name__

limit = int(job.get("max_output_bytes") or 262144)
payload["stdout"] = out.getvalue()[:limit]
payload["stderr"] = err.getvalue()[:limit]

sys.__stdout__.write("\u0000SANDBOX\u0000" + json.dumps(payload, default=str))
sys.__stdout__.flush()
'''

_RESULT_MARKER = "\u0000SANDBOX\u0000"

#: Environment variables that must never be visible to user code.
_SECRET_ENV_PREFIXES = (
    "OPENAI", "ANTHROPIC", "AWS", "AZURE", "GOOGLE", "GCP", "DATABASE",
    "DB_", "SMTP", "AUTH", "SECRET", "TOKEN", "PASSWORD", "API_KEY",
)


def _child_environment() -> Dict[str, str]:
    """A minimal environment with every credential-looking variable removed."""
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONIOENCODING": "utf-8",
        # Prevent the child from importing anything out of the app tree.
        "PYTHONPATH": "",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": tempfile.gettempdir(),
    }
    for key, value in os.environ.items():
        upper = key.upper()
        if any(upper.startswith(prefix) for prefix in _SECRET_ENV_PREFIXES):
            continue
        if key in {"TZ", "TMPDIR"}:
            env[key] = value
    return env


def _kill_process_tree(process: "subprocess.Popen") -> None:
    """Terminate a sandbox child and anything it managed to spawn.

    The child is started with ``start_new_session=True``, so it leads its own
    process group and the whole group can be signalled in one call. Falls back
    to killing the single process where process groups are unavailable.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError, OSError):
        try:
            process.kill()
        except ProcessLookupError:  # pragma: no cover - already reaped
            pass


def _build_job(code: str, inputs: Dict[str, Any], limits: SandboxLimits) -> Dict[str, Any]:
    """Serialise the work order handed to the child on stdin.

    The resource ceilings travel with the job because the child applies them
    to itself (see ``_CHILD_BOOTSTRAP``) rather than inheriting them from a
    ``preexec_fn``, which is not safe to use from a multi-threaded parent.
    """
    return {
        "code": code,
        "inputs": inputs or {},
        "allowed_modules": list(limits.allowed_modules),
        "block_network": limits.block_network,
        "max_output_bytes": limits.max_output_bytes,
        "cpu_seconds": max(1, int(limits.cpu_seconds)),
        "memory_bytes": max(32, int(limits.memory_mb)) * 1024 * 1024,
        "max_file_bytes": max(0, int(limits.max_file_bytes)),
        "max_processes": max(0, int(limits.max_processes)),
    }


def run_python_sandboxed(
    code: str,
    inputs: Optional[Dict[str, Any]] = None,
    limits: Optional[SandboxLimits] = None,
) -> SandboxResult:
    """Execute ``code`` in a resource-limited child process.

    Never raises for user-code errors; failures are reported on the result.
    """
    limits = limits or SandboxLimits()

    if not sandbox_available():  # pragma: no cover - platform dependent
        return SandboxResult(
            ok=False,
            error=(
                "The process sandbox is unavailable on this platform "
                "(POSIX resource limits are required)."
            ),
            error_type="SandboxUnavailable",
        )

    job = _build_job(code, inputs or {}, limits)

    # A private working directory, removed afterwards, so a script that does
    # manage to write a file cannot litter or reach application data.
    workdir = tempfile.mkdtemp(prefix="creator-os-sandbox-")
    try:
        process = subprocess.Popen(
            # -I: isolated mode (ignores PYTHON* env vars and the user site
            # directory). -S: skip `site`, so no sitecustomize hook runs.
            [sys.executable, "-I", "-S", "-c", _CHILD_BOOTSTRAP],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=_child_environment(),
            # New session: the child cannot signal the backend's process group,
            # and killing the parent's group will not orphan it. start_new_session
            # is implemented with posix_spawn/exec-side setsid, so unlike
            # preexec_fn it is safe to use from a threaded process.
            start_new_session=True,
            text=True,
        )
    except Exception as exc:  # pragma: no cover - fork failure
        shutil.rmtree(workdir, ignore_errors=True)
        logger.exception("Failed to start the sandbox process")
        return SandboxResult(
            ok=False,
            error=f"Could not start the sandbox process: {exc}",
            error_type="SandboxStartError",
        )

    try:
        try:
            stdout, stderr = process.communicate(
                json.dumps(job, default=str), timeout=limits.wall_timeout_seconds
            )
        except subprocess.TimeoutExpired:
            # Kill the whole session, not just the direct child. RLIMIT_NPROC
            # makes spawning very hard, but if anything did get forked, killing
            # only the parent would leave an orphan holding the pipe open and
            # communicate() below would block forever.
            _kill_process_tree(process)
            stdout, stderr = process.communicate()
            return SandboxResult(
                ok=False,
                timed_out=True,
                stdout=(stdout or "")[: limits.max_output_bytes],
                stderr=(stderr or "")[: limits.max_output_bytes],
                error=(
                    f"Script exceeded the {limits.wall_timeout_seconds}s wall-clock "
                    "limit and was terminated."
                ),
                error_type="Timeout",
                exit_code=-9,
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    exit_code = process.returncode or 0

    # A negative return code means the child was signalled. SIGKILL(-9) and
    # SIGXCPU(-24) are how the kernel reports our own rlimits being hit.
    if exit_code < 0 or _RESULT_MARKER not in (stdout or ""):
        killed = exit_code in (-9, -24, -25, -6)
        message = (
            "Script exceeded its CPU or memory limit and was terminated by the "
            "kernel."
            if killed
            else f"Sandbox produced no result (exit code {exit_code})."
        )
        return SandboxResult(
            ok=False,
            stdout=(stdout or "")[: limits.max_output_bytes],
            stderr=(stderr or "")[: limits.max_output_bytes],
            error=message,
            error_type="ResourceLimit" if killed else "SandboxFailure",
            exit_code=exit_code,
            killed_by_limit=killed,
        )

    _, _, encoded = (stdout or "").partition(_RESULT_MARKER)
    try:
        payload = json.loads(encoded)
    except (ValueError, TypeError):
        return SandboxResult(
            ok=False,
            error="Sandbox returned a malformed result payload.",
            error_type="SandboxFailure",
            exit_code=exit_code,
            stderr=(stderr or "")[: limits.max_output_bytes],
        )

    error_type = payload.get("error_type")
    return SandboxResult(
        ok=bool(payload.get("ok")),
        result=payload.get("result"),
        variables=payload.get("variables") or {},
        stdout=payload.get("stdout", ""),
        stderr=payload.get("stderr", ""),
        error=payload.get("error"),
        error_type=error_type,
        exit_code=exit_code,
        # A MemoryError inside the child is how RLIMIT_AS surfaces when the
        # allocation is refused rather than the process being killed outright.
        # Report it as a limit breach so callers can tell it apart from an
        # ordinary bug in the script.
        killed_by_limit=error_type == "MemoryError",
    )


def sandbox_status() -> Dict[str, Any]:
    """Introspection payload for ``GET /api/system/info``."""
    from app.infrastructure.config.settings import settings

    return {
        "available": sandbox_available(),
        "enabled": bool(settings.SCRIPT_SANDBOX_ENABLED),
        "platform_supported": RESOURCE_LIMITS_AVAILABLE,
        "cpu_seconds": settings.SCRIPT_SANDBOX_CPU_SECONDS,
        "memory_mb": settings.SCRIPT_SANDBOX_MEMORY_MB,
        "block_network": settings.SCRIPT_SANDBOX_BLOCK_NETWORK,
        "allowed_modules": list(settings.SCRIPT_SANDBOX_ALLOWED_MODULES),
        "quota_per_run": settings.SCRIPT_EXECUTION_QUOTA_PER_RUN,
        "is_security_boundary": False,
        "notes": (
            "Process isolation with POSIX resource limits. Defence in depth, "
            "not a security boundary: a CPython escape yields the backend "
            "user's privileges. Run untrusted code in a container instead."
        ),
    }


__all__ = [
    "SandboxLimits",
    "SandboxResult",
    "run_python_sandboxed",
    "sandbox_available",
    "sandbox_status",
]
