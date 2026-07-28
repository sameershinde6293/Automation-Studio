"""Data, scripting and IO nodes: python, javascript, database, email, file, folder.

Security posture (read this before enabling anything here)
----------------------------------------------------------
The ``python`` and ``javascript`` nodes execute user-supplied code.

* ``python`` (M5) runs in a **separate OS process** with POSIX resource limits
  (``RLIMIT_CPU``, ``RLIMIT_AS``, ``RLIMIT_FSIZE``, ``RLIMIT_NPROC``), a
  scrubbed environment, a private temp working directory, an import allowlist
  and a PEP 578 audit hook that refuses file, process, network and dynamic-code
  operations. See ``app.services.security.sandbox``. This is a substantial
  hardening over the pre-M5 in-process ``exec``: a CPU-bound infinite loop and
  a memory bomb are now contained by the kernel rather than taking down the
  backend. It is **defence in depth, not a security boundary** — a CPython
  escape still yields the backend user's OS privileges.
* ``javascript`` shells out to a local Node.js binary with **no isolation**
  beyond a timeout and the OS user's own permissions. It has not been
  sandboxed in M5; see docs/SECURITY.md.

Both are therefore **disabled by default** (``ALLOW_PYTHON_EXECUTOR`` /
``ALLOW_JAVASCRIPT_EXECUTOR``) and must only be enabled when every workflow
author is already trusted with local code execution. The same reasoning applies
to ``database`` (``ALLOW_DATABASE_EXECUTOR``).

File and folder nodes are always confined to ``MEDIA_ROOT`` via the M2
``resolve_media_path`` helper, so they cannot traverse out of the sandbox.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
from typing import Any, Dict, List, Optional

from app.core.errors import SecurityError, ValidationError
from app.infrastructure.config.settings import settings
from app.services.workflow.executors import (
    coerce_number,
    render_template,
    render_value,
)
from app.services.workflow.runtime import (
    FieldSpec,
    NodeContext,
    NodeErrorCode,
    NodeExecutionError,
    NodeSchema,
    RuntimeNodeExecutor,
)

# --------------------------------------------------------------------------- #
# Python
# --------------------------------------------------------------------------- #
#: Builtins exposed to the python node. Deliberately excludes __import__,
#: open, eval, exec, compile, globals, locals, vars, getattr and setattr.
SAFE_BUILTINS: Dict[str, Any] = {
    "abs": abs, "all": all, "any": any, "bool": bool, "bytes": bytes,
    "chr": chr, "dict": dict, "divmod": divmod, "enumerate": enumerate,
    "filter": filter, "float": float, "format": format, "frozenset": frozenset,
    "hash": hash, "hex": hex, "int": int, "isinstance": isinstance,
    "issubclass": issubclass, "iter": iter, "len": len, "list": list,
    "map": map, "max": max, "min": min, "next": next, "oct": oct, "ord": ord,
    "pow": pow, "print": print, "range": range, "repr": repr, "reversed": reversed,
    "round": round, "set": set, "slice": slice, "sorted": sorted, "str": str,
    "sum": sum, "tuple": tuple, "type": type, "zip": zip,
    "True": True, "False": False, "None": None,
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "IndexError": IndexError,
}

#: Modules injected as pre-imported names (no import statement is possible).
SAFE_MODULES = {"json": json, "math": math, "re": re}

#: Source-level pre-checks, applied before the script is handed to the sandbox.
#:
#: These are a *usability and defence-in-depth* layer, not the security
#: boundary: a blocklist over source text is trivially bypassable
#: (``getattr`` spelled ``__getattr''__`` and so on). Their real value is that
#: an obviously dangerous snippet fails immediately, with a precise reason,
#: instead of spawning a process and failing opaquely 30ms later.
#:
#: The M5 sandbox is what actually enforces the policy at runtime, via an
#: import allowlist and a PEP 578 audit hook. ``import`` is therefore no longer
#: rejected here — the sandbox permits a curated module list, which is strictly
#: more useful and no less safe.
_FORBIDDEN_PATTERNS = (
    (re.compile(r"__\w+__"), "dunder access is not allowed"),
    (re.compile(r"\bopen\s*\("), "file access is not allowed"),
    (re.compile(r"\b(eval|exec|compile)\s*\("), "dynamic evaluation is not allowed"),
    (re.compile(r"\bglobals\s*\(|\blocals\s*\(|\bvars\s*\("), "scope introspection is not allowed"),
    (re.compile(r"\bgetattr\s*\(|\bsetattr\s*\(|\bdelattr\s*\("), "attribute reflection is not allowed"),
    (re.compile(r"\bsubprocess\b|\bos\s*\.|\bsys\s*\."), "system access is not allowed"),
)

#: Applied only on the legacy in-process path, which has no import machinery at
#: all and so must reject imports outright.
_LEGACY_IMPORT_PATTERN = (
    re.compile(r"\bimport\b"),
    "import statements are not allowed",
)


class PythonNode(RuntimeNodeExecutor):
    """Runs a restricted Python snippet. Disabled by default."""

    label = "Python"
    category = "script"
    description = (
        "Executes a Python snippet in a resource-limited child process "
        "(CPU, memory and filesystem limits; import allowlist; no network). "
        "Hardened but not a full security boundary - disabled by default."
    )
    aliases = ("python_node", "python_script")
    requires_flag = "ALLOW_PYTHON_EXECUTOR"
    schema = NodeSchema(
        inputs=[
            FieldSpec(
                "code",
                "string",
                required=True,
                description="Python source. Assign to 'result' to return a value.",
            ),
            FieldSpec("inputs", "object", description="Values bound as local names"),
            FieldSpec(
                "timeout", "number", minimum=1.0, maximum=300.0, default=30.0
            ),
        ],
        outputs=[
            FieldSpec("result", "any", description="Value of the 'result' variable"),
            FieldSpec("stdout", "string"),
            FieldSpec("variables", "object", description="Public locals after the run"),
        ],
    )

    @staticmethod
    def _reject_forbidden(code: str, *, include_imports: bool = False) -> None:
        """Fast source-level rejection of obviously dangerous snippets.

        ``include_imports`` is set on the legacy in-process path, which cannot
        support imports at all. The sandboxed path leaves import policy to the
        sandbox's allowlist.
        """
        patterns = _FORBIDDEN_PATTERNS
        if include_imports:
            patterns = patterns + (_LEGACY_IMPORT_PATTERN,)
        for pattern, reason in patterns:
            if pattern.search(code):
                raise SecurityError(
                    f"Python node rejected: {reason}.",
                    details={"reason": reason},
                )

    def _execute_sync(self, code: str, local_vars: Dict[str, Any]) -> Dict[str, Any]:
        import io
        from contextlib import redirect_stdout

        scope: Dict[str, Any] = dict(SAFE_MODULES)
        scope.update(local_vars)
        scope["result"] = None
        globals_dict: Dict[str, Any] = {"__builtins__": dict(SAFE_BUILTINS)}

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exec(compile(code, "<workflow-python-node>", "exec"), globals_dict, scope)  # noqa: S102

        public = {
            key: value
            for key, value in scope.items()
            if not key.startswith("_")
            and key not in SAFE_MODULES
            and isinstance(value, (str, int, float, bool, list, dict, type(None)))
        }
        return {
            "result": scope.get("result"),
            "stdout": buffer.getvalue()[:64000],
            "variables": public,
        }

    async def _run_sandboxed(
        self, code: str, bound: Dict[str, Any], timeout: float
    ) -> Dict[str, Any]:
        """Execute in a resource-limited child process (M5, the default path)."""
        from app.services.security.sandbox import (
            SandboxLimits,
            run_python_sandboxed,
        )

        limits = SandboxLimits.from_settings(settings, wall_timeout=timeout)
        outcome = await asyncio.to_thread(
            run_python_sandboxed, code, bound, limits
        )

        if outcome.ok:
            return {
                "result": outcome.result,
                "stdout": outcome.stdout,
                "variables": outcome.variables,
            }

        if outcome.timed_out:
            raise NodeExecutionError(
                f"Python node timed out after {timeout}s.",
                code=NodeErrorCode.TIMEOUT,
                details={"stdout": outcome.stdout[:2000]},
            )
        if outcome.killed_by_limit:
            raise NodeExecutionError(
                outcome.error
                or "Python node exceeded its CPU or memory limit.",
                code=NodeErrorCode.RUNTIME,
                details={
                    "limit": "cpu_or_memory",
                    "cpu_seconds": limits.cpu_seconds,
                    "memory_mb": limits.memory_mb,
                },
            )
        if outcome.error_type in {"PermissionError", "ImportError"}:
            # The sandbox refused a restricted operation. This is a policy
            # violation, not a transient fault, so it must not be retried.
            raise SecurityError(
                outcome.error or "Operation blocked by the script sandbox.",
                details={"error_type": outcome.error_type},
            )
        if outcome.error_type == "SyntaxError":
            raise ValidationError(outcome.error or "Python node has a syntax error.")

        raise NodeExecutionError(
            outcome.error or "Python node failed.",
            code=NodeErrorCode.RUNTIME,
            details={
                "error_type": outcome.error_type,
                "stdout": outcome.stdout[:2000],
            },
        )

    @staticmethod
    def _check_quota(context: Any) -> None:
        """Enforce the per-execution script invocation quota.

        Without this, a loop node wrapping a script node could spawn an
        unbounded number of sandbox processes in a single run.
        """
        quota = int(getattr(settings, "SCRIPT_EXECUTION_QUOTA_PER_RUN", 0) or 0)
        if quota <= 0 or not isinstance(context, dict):
            return
        used = int(context.get("__script_invocations__", 0)) + 1
        context["__script_invocations__"] = used
        if used > quota:
            raise SecurityError(
                f"Script execution quota exceeded: {quota} script node runs "
                "are permitted per workflow execution.",
                details={"quota": quota, "used": used},
            )

    async def run(self, node, context, config) -> Any:
        code = str(config.get("code") or "")
        if not code.strip():
            raise ValidationError("Python node requires non-empty 'code'.")

        # Fast source-level pre-check (see _FORBIDDEN_PATTERNS). Runs on both
        # paths so an obviously dangerous snippet is refused before a process
        # is spawned, and so the SecurityError contract is identical either way.
        self._reject_forbidden(code)
        self._check_quota(context)

        bound = render_value(config.get("inputs") or {}, context)
        if not isinstance(bound, dict):
            raise ValidationError("Python node 'inputs' must be an object.")
        if isinstance(context, NodeContext):
            bound.setdefault("vars", dict(context.variables))

        timeout = coerce_number(
            config.get("timeout"), settings.PYTHON_EXECUTOR_TIMEOUT_SECONDS
        )
        timeout = max(1.0, min(timeout, 300.0))

        # M5: prefer the process sandbox. It enforces CPU and memory limits the
        # in-process path cannot: `while True: pass` used to pin a core for the
        # life of the backend because a thread cannot be cancelled, and a large
        # allocation could OOM the whole service.
        from app.services.security.sandbox import sandbox_available

        if settings.SCRIPT_SANDBOX_ENABLED and sandbox_available():
            return await self._run_sandboxed(code, bound, timeout)

        # Fallback: restricted in-process exec (pre-M5 behaviour). Only reached
        # when the sandbox is explicitly disabled or the platform lacks POSIX
        # resource limits. The source-level blocklist applies here only, since
        # it is all this path has.
        self._reject_forbidden(code, include_imports=True)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._execute_sync, code, bound), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            raise NodeExecutionError(
                f"Python node timed out after {timeout}s.",
                code=NodeErrorCode.TIMEOUT,
            ) from exc
        except SecurityError:
            raise
        except SyntaxError as exc:
            raise ValidationError(
                f"Python node has a syntax error: {exc}",
                details={"line": exc.lineno},
            ) from exc
        except Exception as exc:
            raise NodeExecutionError(
                f"Python node raised {type(exc).__name__}: {exc}",
                code=NodeErrorCode.RUNTIME,
            ) from exc


class JavaScriptNode(RuntimeNodeExecutor):
    """Runs a JavaScript snippet via a local Node.js binary. Disabled by default."""

    label = "JavaScript"
    category = "script"
    description = (
        "Executes JavaScript with a local Node.js binary. NOT sandboxed - "
        "disabled by default."
    )
    aliases = ("javascript_node", "js", "node_script")
    requires_flag = "ALLOW_JAVASCRIPT_EXECUTOR"
    schema = NodeSchema(
        inputs=[
            FieldSpec(
                "code",
                "string",
                required=True,
                description="JS source. Assign to 'result' or return a value.",
            ),
            FieldSpec("inputs", "object", description="Exposed as the 'input' object"),
            FieldSpec("timeout", "number", minimum=1.0, maximum=300.0, default=30.0),
        ],
        outputs=[
            FieldSpec("result", "any"),
            FieldSpec("stdout", "string"),
            FieldSpec("exit_code", "integer"),
        ],
    )

    WRAPPER = """
'use strict';
const input = JSON.parse(process.argv[2] || '{}');
let result = null;
(function() {
%s
})();
try {
  process.stdout.write('\\u0000RESULT\\u0000' + JSON.stringify(result === undefined ? null : result));
} catch (e) {
  process.stdout.write('\\u0000RESULT\\u0000null');
}
"""

    async def run(self, node, context, config) -> Any:
        code = str(config.get("code") or "")
        if not code.strip():
            raise ValidationError("JavaScript node requires non-empty 'code'.")

        binary = settings.JAVASCRIPT_BINARY
        if not shutil.which(binary):
            raise NodeExecutionError(
                f"JavaScript runtime {binary!r} was not found on PATH.",
                code=NodeErrorCode.DISABLED,
                details={"binary": binary},
            )

        bound = render_value(config.get("inputs") or {}, context)
        if not isinstance(bound, dict):
            raise ValidationError("JavaScript node 'inputs' must be an object.")

        # `result` is declared in the wrapper's outer scope; the user body is
        # spliced into an IIFE so `return` is legal.
        indented = "\n".join("  " + line for line in code.splitlines())
        script = self.WRAPPER % indented

        timeout = coerce_number(
            config.get("timeout"), settings.JAVASCRIPT_EXECUTOR_TIMEOUT_SECONDS
        )
        timeout = max(1.0, min(timeout, 300.0))

        process = await asyncio.create_subprocess_exec(
            binary,
            "-e",
            script,
            "--",
            json.dumps(bound, default=str),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            try:
                process.kill()
            except ProcessLookupError:  # pragma: no cover - race
                pass
            await process.wait()
            raise NodeExecutionError(
                f"JavaScript node timed out after {timeout}s.",
                code=NodeErrorCode.TIMEOUT,
            ) from exc

        out = stdout.decode(errors="replace")
        err = stderr.decode(errors="replace")[:16000]

        if process.returncode != 0:
            raise NodeExecutionError(
                f"JavaScript node exited with code {process.returncode}: {err[:500]}",
                code=NodeErrorCode.RUNTIME,
                details={"exit_code": process.returncode, "stderr": err[:2000]},
            )

        marker = "\u0000RESULT\u0000"
        result: Any = None
        if marker in out:
            head, _, tail = out.partition(marker)
            out = head
            try:
                result = json.loads(tail)
            except json.JSONDecodeError:
                result = tail

        return {
            "result": result,
            "stdout": out[:64000],
            "stderr": err,
            "exit_code": process.returncode,
        }


class DatabaseNode(RuntimeNodeExecutor):
    """Runs SQL against the application database. Disabled by default."""

    label = "Database"
    category = "data"
    description = (
        "Executes SQL against the application database. Disabled by default; "
        "read-only unless 'allow_write' is set."
    )
    aliases = ("database_node", "sql")
    requires_flag = "ALLOW_DATABASE_EXECUTOR"
    schema = NodeSchema(
        inputs=[
            FieldSpec("query", "string", required=True, description="SQL statement"),
            FieldSpec("parameters", "object", description="Bound parameters"),
            FieldSpec(
                "allow_write",
                "boolean",
                default=False,
                description="Permit INSERT/UPDATE/DELETE",
            ),
            FieldSpec("max_rows", "integer", minimum=1, maximum=10000),
        ],
        outputs=[
            FieldSpec("rows", "array"),
            FieldSpec("row_count", "integer"),
            FieldSpec("columns", "array"),
        ],
    )

    WRITE_KEYWORDS = (
        "insert", "update", "delete", "drop", "alter", "create", "truncate",
        "replace", "grant", "revoke", "attach", "detach", "pragma", "vacuum",
    )

    def _run_sync(
        self, query: str, params: Dict[str, Any], max_rows: int
    ) -> Dict[str, Any]:
        from sqlalchemy import text

        from app.infrastructure.database.database import SessionLocal

        with SessionLocal() as db:
            result = db.execute(text(query), params)
            if result.returns_rows:
                columns = list(result.keys())
                rows = [
                    dict(zip(columns, row))
                    for row in result.fetchmany(max_rows)
                ]
                return {
                    "rows": rows,
                    "row_count": len(rows),
                    "columns": columns,
                    "truncated": len(rows) >= max_rows,
                }
            db.commit()
            return {
                "rows": [],
                "row_count": result.rowcount if result.rowcount is not None else 0,
                "columns": [],
                "truncated": False,
            }

    async def run(self, node, context, config) -> Any:
        query = render_template(str(config.get("query") or ""), context).strip()
        if not query:
            raise ValidationError("Database node requires a non-empty 'query'.")

        # Reject stacked statements outright; one node = one statement.
        if ";" in query.rstrip().rstrip(";"):
            raise SecurityError(
                "Database node rejects multiple statements in one query.",
            )

        lowered = query.lstrip().lower()
        is_write = any(lowered.startswith(word) for word in self.WRITE_KEYWORDS)
        if is_write and not config.get("allow_write"):
            raise SecurityError(
                "This query modifies data; set 'allow_write' to true to permit it.",
                details={"statement": lowered.split()[0]},
            )

        params = render_value(config.get("parameters") or {}, context)
        if not isinstance(params, dict):
            raise ValidationError("Database node 'parameters' must be an object.")

        max_rows = int(
            coerce_number(
                config.get("max_rows"), settings.DATABASE_EXECUTOR_MAX_ROWS
            )
        )
        max_rows = max(1, min(max_rows, 10000))

        try:
            return await asyncio.to_thread(self._run_sync, query, params, max_rows)
        except (SecurityError, ValidationError):
            raise
        except Exception as exc:
            raise NodeExecutionError(
                f"Database query failed: {type(exc).__name__}: {exc}",
                code=NodeErrorCode.RUNTIME,
            ) from exc


class EmailNode(RuntimeNodeExecutor):
    """Sends an email over SMTP.

    With no ``SMTP_HOST`` configured the node runs in **dry-run** mode: it
    renders and validates the message and reports ``sent=False`` rather than
    pretending to have delivered it.
    """

    label = "Email"
    category = "integration"
    description = "Sends an email via SMTP (dry-run when SMTP is not configured)."
    aliases = ("email_node", "send_email", "mail")
    schema = NodeSchema(
        inputs=[
            FieldSpec("to", "array", required=True, description="Recipient addresses"),
            FieldSpec("subject", "string", required=True),
            FieldSpec("body", "string", required=True),
            FieldSpec("from_address", "string"),
            FieldSpec("cc", "array"),
            FieldSpec("bcc", "array"),
            FieldSpec("html", "boolean", default=False),
        ],
        outputs=[
            FieldSpec("sent", "boolean"),
            FieldSpec("dry_run", "boolean"),
            FieldSpec("recipients", "array"),
        ],
    )

    ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    def _normalise_addresses(self, value: Any, context: Any, field_name: str) -> List[str]:
        rendered = render_value(value, context)
        if rendered is None or rendered == "":
            return []
        if isinstance(rendered, str):
            candidates = [
                part.strip() for part in re.split(r"[,;]", rendered) if part.strip()
            ]
        elif isinstance(rendered, (list, tuple)):
            candidates = [str(item).strip() for item in rendered if str(item).strip()]
        else:
            raise ValidationError(f"Email node '{field_name}' must be a list or string.")
        for address in candidates:
            if not self.ADDRESS_RE.match(address):
                raise ValidationError(
                    f"Invalid email address in '{field_name}': {address!r}.",
                    details={"address": address},
                )
        return candidates

    def _send_sync(
        self,
        *,
        sender: str,
        recipients: List[str],
        subject: str,
        body: str,
        html: bool,
        cc: List[str],
        to: Optional[List[str]] = None,
    ) -> None:
        """Send one message.

        ``to``/``cc`` are the **visible headers**; ``recipients`` is the
        **SMTP envelope** (to + cc + bcc). Keeping the two separate is what
        makes bcc blind, and is why the envelope is passed to ``send_message``
        explicitly rather than left to be derived from the headers.
        """
        import smtplib
        from email.message import EmailMessage

        # Fall back to the envelope only when no explicit To list is supplied,
        # preserving the previous signature for any direct caller.
        visible_to = list(to) if to is not None else list(recipients)

        message = EmailMessage()
        message["From"] = sender
        message["To"] = ", ".join(visible_to)
        if cc:
            message["Cc"] = ", ".join(cc)
        message["Subject"] = subject
        if html:
            message.set_content(re.sub(r"<[^>]+>", "", body))
            message.add_alternative(body, subtype="html")
        else:
            message.set_content(body)

        with smtplib.SMTP(
            settings.SMTP_HOST,
            settings.SMTP_PORT,
            timeout=settings.SMTP_TIMEOUT_SECONDS,
        ) as smtp:
            if settings.SMTP_USE_TLS:
                smtp.starttls()
            if settings.SMTP_USERNAME:
                smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            # The envelope recipients are passed explicitly. Without this,
            # smtplib derives them from the To/Cc/Bcc *headers*, and since a
            # Bcc header is deliberately never written (that is what makes it
            # blind), every bcc address was silently dropped while the node
            # still reported it in `recipients` and returned sent=True.
            smtp.send_message(message, from_addr=sender, to_addrs=recipients)

    async def run(self, node, context, config) -> Any:
        to = self._normalise_addresses(config.get("to"), context, "to")
        if not to:
            raise ValidationError("Email node requires at least one recipient.")
        cc = self._normalise_addresses(config.get("cc"), context, "cc")
        bcc = self._normalise_addresses(config.get("bcc"), context, "bcc")

        subject = render_template(str(config.get("subject") or ""), context)
        body = render_template(str(config.get("body") or ""), context)
        sender = (
            render_template(str(config.get("from_address") or ""), context).strip()
            or settings.SMTP_FROM_ADDRESS
            or settings.SMTP_USERNAME
        )
        html = bool(config.get("html"))
        recipients = to + cc + bcc

        if not settings.SMTP_HOST:
            # Dry run: render and validate but never claim delivery.
            return {
                "sent": False,
                "dry_run": True,
                "reason": "SMTP_HOST is not configured.",
                "recipients": recipients,
                "subject": subject,
                "body_preview": body[:500],
            }

        if not sender:
            raise ValidationError(
                "Email node requires 'from_address' or SMTP_FROM_ADDRESS."
            )

        try:
            await asyncio.to_thread(
                self._send_sync,
                sender=sender,
                recipients=recipients,
                to=to,
                subject=subject,
                body=body,
                html=html,
                cc=cc,
            )
        except Exception as exc:
            raise NodeExecutionError(
                f"SMTP delivery failed: {type(exc).__name__}: {exc}",
                code=NodeErrorCode.NETWORK,
                details={"host": settings.SMTP_HOST},
            ) from exc

        return {
            "sent": True,
            "dry_run": False,
            "recipients": recipients,
            "subject": subject,
        }


class FileNode(RuntimeNodeExecutor):
    """Reads, writes, appends or deletes a file inside ``MEDIA_ROOT``."""

    label = "File"
    category = "io"
    description = "Reads or writes a file, confined to the media storage root."
    aliases = ("file_node", "file_io")
    schema = NodeSchema(
        inputs=[
            FieldSpec(
                "path",
                "string",
                required=True,
                description="Path relative to MEDIA_ROOT",
            ),
            FieldSpec(
                "operation",
                "string",
                default="read",
                enum=["read", "write", "append", "delete", "exists"],
            ),
            FieldSpec("content", "string", description="Content for write/append"),
            FieldSpec("encoding", "string", default="utf-8"),
            FieldSpec(
                "max_bytes", "integer", minimum=1, maximum=50 * 1024 * 1024,
                default=1024 * 1024,
            ),
        ],
        outputs=[
            FieldSpec("path", "string"),
            FieldSpec("content", "string", description="File content on read"),
            FieldSpec("size_bytes", "integer"),
            FieldSpec("exists", "boolean"),
        ],
    )

    def _run_sync(self, config: Dict[str, Any], relative: str) -> Dict[str, Any]:
        from app.services.media.storage import resolve_media_path

        operation = str(config.get("operation") or "read").lower()
        encoding = str(config.get("encoding") or "utf-8")
        max_bytes = int(config.get("max_bytes") or 1024 * 1024)

        if operation == "exists":
            try:
                path = resolve_media_path(relative, must_exist=False)
                return {"path": relative, "exists": path.exists()}
            except Exception:
                return {"path": relative, "exists": False}

        if operation == "read":
            path = resolve_media_path(relative, must_exist=True)
            data = path.read_bytes()[:max_bytes]
            return {
                "path": relative,
                "content": data.decode(encoding, errors="replace"),
                "size_bytes": path.stat().st_size,
                "exists": True,
                "truncated": path.stat().st_size > max_bytes,
            }

        if operation in {"write", "append"}:
            path = resolve_media_path(relative, must_exist=False)
            path.parent.mkdir(parents=True, exist_ok=True)
            content = str(config.get("content") or "")
            encoded = content.encode(encoding, errors="replace")
            if len(encoded) > max_bytes:
                raise ValidationError(
                    f"Content exceeds max_bytes ({len(encoded)} > {max_bytes}).",
                )
            mode = "ab" if operation == "append" else "wb"
            with open(path, mode) as handle:
                handle.write(encoded)
            return {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "exists": True,
                "written_bytes": len(encoded),
            }

        if operation == "delete":
            path = resolve_media_path(relative, must_exist=False)
            existed = path.exists()
            if existed:
                path.unlink()
            return {"path": relative, "deleted": existed, "exists": False}

        raise ValidationError(f"Unsupported file operation {operation!r}.")

    async def run(self, node, context, config) -> Any:
        relative = render_template(str(config.get("path") or ""), context).strip()
        if not relative:
            raise ValidationError("File node requires a 'path'.")
        try:
            return await asyncio.to_thread(self._run_sync, config, relative)
        except (SecurityError, ValidationError):
            raise
        except FileNotFoundError as exc:
            raise NodeExecutionError(
                f"File not found: {relative}",
                code=NodeErrorCode.NOT_FOUND,
            ) from exc
        except OSError as exc:
            raise NodeExecutionError(
                f"File operation failed: {exc}", code=NodeErrorCode.RUNTIME
            ) from exc


class FolderNode(RuntimeNodeExecutor):
    """Lists, creates or deletes a directory inside ``MEDIA_ROOT``."""

    label = "Folder"
    category = "io"
    description = "Lists or manages a directory, confined to the media storage root."
    aliases = ("folder_node", "directory")
    schema = NodeSchema(
        inputs=[
            FieldSpec("path", "string", default="", description="Path relative to MEDIA_ROOT"),
            FieldSpec(
                "operation",
                "string",
                default="list",
                enum=["list", "create", "delete", "exists"],
            ),
            FieldSpec("pattern", "string", default="*", description="Glob filter for 'list'"),
            FieldSpec("recursive", "boolean", default=False),
            FieldSpec("max_entries", "integer", default=500, minimum=1, maximum=10000),
        ],
        outputs=[
            FieldSpec("path", "string"),
            FieldSpec("entries", "array"),
            FieldSpec("count", "integer"),
            FieldSpec("exists", "boolean"),
        ],
    )

    def _run_sync(self, config: Dict[str, Any], relative: str) -> Dict[str, Any]:
        from app.services.media.storage import media_root, resolve_media_path

        operation = str(config.get("operation") or "list").lower()
        root = media_root()
        path = resolve_media_path(relative, must_exist=False) if relative else root

        if operation == "exists":
            return {"path": relative, "exists": path.is_dir()}

        if operation == "create":
            path.mkdir(parents=True, exist_ok=True)
            return {"path": relative, "created": True, "exists": True}

        if operation == "delete":
            existed = path.is_dir()
            if existed:
                if path.resolve() == root.resolve():
                    raise SecurityError("Refusing to delete the media storage root.")
                shutil.rmtree(path)
            return {"path": relative, "deleted": existed, "exists": False}

        if operation == "list":
            if not path.is_dir():
                raise NodeExecutionError(
                    f"Directory not found: {relative or '.'}",
                    code=NodeErrorCode.NOT_FOUND,
                )
            pattern = str(config.get("pattern") or "*")
            limit = int(config.get("max_entries") or 500)
            iterator = (
                path.rglob(pattern) if config.get("recursive") else path.glob(pattern)
            )
            entries: List[Dict[str, Any]] = []
            for item in iterator:
                if len(entries) >= limit:
                    break
                try:
                    stat = item.stat()
                except OSError:  # pragma: no cover - race
                    continue
                entries.append(
                    {
                        "name": item.name,
                        "path": str(item.relative_to(root)),
                        "is_dir": item.is_dir(),
                        "size_bytes": stat.st_size,
                    }
                )
            entries.sort(key=lambda e: (not e["is_dir"], e["name"]))
            return {
                "path": relative,
                "entries": entries,
                "count": len(entries),
                "exists": True,
                "truncated": len(entries) >= limit,
            }

        raise ValidationError(f"Unsupported folder operation {operation!r}.")

    async def run(self, node, context, config) -> Any:
        relative = render_template(str(config.get("path") or ""), context).strip()
        try:
            return await asyncio.to_thread(self._run_sync, config, relative)
        except (SecurityError, ValidationError, NodeExecutionError):
            raise
        except OSError as exc:
            raise NodeExecutionError(
                f"Folder operation failed: {exc}", code=NodeErrorCode.RUNTIME
            ) from exc
