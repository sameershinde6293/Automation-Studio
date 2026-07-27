# Security

Creator OS v1.1 · last updated 2026-07-26 (M6)

This document describes what the platform's security controls **do** and, just
as importantly, what they **do not** do. Claims here are backed by tests in
`backend/tests/m5/` and `backend/tests/m6/`; where a control is incomplete it
is stated plainly rather than softened.

> **M6 second security review.** M6 re-tested every M5 control against a live
> production-configured server and found **one real bypass**: the `/api/v1`
> router alias did not inherit four path-prefix controls, so the stricter
> credential-endpoint rate limit could be evaded by inserting `/v1` into the
> URL (measured: 14 consecutive login attempts, never throttled). Fixed, and
> both mounts now share a single rate-limit bucket — asserted by test.
>
> RBAC was re-verified as **not** affected: authorization is enforced by
> router-level dependencies that apply to both mounts. Anonymous access and
> `viewer`-role writes are rejected identically on `/api` and `/api/v1`.
>
> Bypasses attempted and confirmed still impossible: `alg:none` and
> RS256→HS256 JWT confusion, replaying an access token as a refresh token,
> rate-limit evasion by alternating mounts, credential caching by an
> intermediary, and internal detail leaking through error responses. Full
> matrix in `M6_VALIDATION_REPORT.md` §8.

---

## 1. Threat model

Creator OS began as a single-user, local-first desktop application and is
becoming a multi-user platform. Those have different threat models, and the
product supports both:

| Mode | `AUTH_ENABLED` | Assumption |
| --- | --- | --- |
| **Desktop** (default) | `false` | One trusted user on one machine. The API binds locally and every caller is a local admin. |
| **Platform** | `true` | Multiple users with different privileges, reachable over a network. |

**Production requires platform mode.** Booting with `ENVIRONMENT=production`
and `AUTH_ENABLED=false` is a startup **error** and the process refuses to
start (`app/core/startup.py`).

### In scope
Unauthenticated access, privilege escalation between roles, credential theft
and replay, CSRF, SSRF, path traversal, injection, resource exhaustion, and
containment of user-supplied workflow scripts.

### Out of scope
An attacker with the backend user's OS privileges, a malicious administrator,
supply-chain compromise, and physical access. **Untrusted multi-tenant code
execution is explicitly out of scope** — see §5.

---

## 2. Authentication

Implemented in `app/services/security/`.

| Control | Implementation |
| --- | --- |
| Password storage | PBKDF2-HMAC-SHA256, 600,000 iterations, 16-byte random salt per password. Parameters are embedded in the hash, so they can be raised later; `needs_rehash` upgrades a hash on the next successful login. |
| Password policy | Minimum 12 characters, length favoured over character-class rules. Input capped at 1 KiB so PBKDF2 cannot be used for CPU exhaustion. |
| Tokens | HS256 JWT. The `alg` header is checked **before** verification, so `alg: none` and RS256→HS256 confusion are rejected. Signatures compared with `hmac.compare_digest`. `exp`, `nbf`, `iss`, `aud` and `typ` are all validated. |
| Token separation | A refresh token cannot be replayed as an access token (`typ` enforced). |
| Refresh rotation | Using a refresh token revokes it and issues a new one. Presenting a **consumed** token revokes every session for that user, on the assumption it was stolen. |
| Revocation | Refresh sessions are server-side rows, so logout, password change and deactivation take effect immediately. |
| API keys | 256 bits of entropy, `cos_` prefixed. Only a SHA-256 digest is stored; the plaintext is shown once. Optional expiry and scopes. |
| Lockout | 5 consecutive failures locks an account for 15 minutes. |
| Enumeration resistance | Unknown username and wrong password return an identical error, and a dummy hash is verified for unknown users so timing does not differ. |

**Authorization is read from the database, not the token.** Demoting or
disabling a user takes effect on their next request rather than at token
expiry.

### Known weaknesses
- **Access tokens cannot be revoked individually** before their 15-minute
  expiry. Only refresh sessions are stateful. Shorten
  `AUTH_ACCESS_TOKEN_TTL_SECONDS` if that window is unacceptable.
- **No MFA, no SSO/OIDC, no password reset flow.** An administrator must reset
  a forgotten password.
- **Lockout is per-account**, so it does not stop password spraying across many
  accounts from one source. The auth rate limit partially covers this.

---

## 3. Authorization (RBAC)

Four roles, defined once in `app/services/enterprise/auth.py`:

| Role | Permissions |
| --- | --- |
| `admin` | read, write, execute, manage_users, manage_plugins, manage_settings, view_audit |
| `editor` | read, write, execute |
| `operator` | read, execute |
| `viewer` | read |

Enforced by FastAPI dependencies in `app/api/dependencies.py`
(`require_read`, `require_write`, `require_execute`, `require_manage_users`, …).

Authorization is applied **at router level** via
`APIRouter(dependencies=[...])` rather than per endpoint:

| Router | Safe methods | Mutations |
| --- | --- | --- |
| workflows, projects, ai, media | `read` | `write` |
| executions | `read` | `execute` |
| plugins | `read` | `manage_plugins` |
| enterprise | per route: `view_audit` to read the log, `manage_settings` to write |

Triggering or cancelling a workflow run additionally requires `execute`.

This is deliberate and fails closed. The M5 self-audit found that annotating
endpoints individually had left **7 of 9 routers entirely unprotected** — an
endpoint without a decorator is silently public and nothing flags it. A
router-level default protects a new route the moment it is added, and
`tests/m5/test_endpoint_authorization_m5.py::TestRouteCoverage` walks the live
route table and fails if any non-public route lacks an authorization
dependency.

**Before M5 this model was decorative** — `require_permission` existed but no
route ever called it. That is the specific regression the tests in
`tests/m5/test_authorization_m5.py` exist to prevent.

Escalation is structurally prevented:
- **API key scopes intersect** the owner's role permissions. A key scoped
  `manage_users` held by a `viewer` grants only `read`.
- An administrator **cannot deactivate or demote themselves**, so an instance
  cannot be left with no one able to manage users.

### Known weakness
RBAC is **global, not per-resource**. Any `editor` can modify any workflow;
there is no ownership, no per-workflow ACL and no tenancy. Creator OS is
suitable for a team that mutually trusts each other at its permission tier,
not for isolating customers from one another.

---

## 4. Transport and request handling

| Control | Default | Notes |
| --- | --- | --- |
| Security headers | on | `nosniff`, `DENY`, `no-referrer`, CSP, COOP, CORP, Permissions-Policy |
| HSTS | off | Enable (`SECURITY_HSTS_ENABLED`) once TLS terminates in front |
| CORS | localhost origins | Wildcard with credentials is a startup error |
| Trusted hosts | `["*"]` (off) | Set `ALLOWED_HOSTS` in production to block host-header injection and DNS rebinding |
| CSRF | on | Double-submit cookie. Header-authenticated requests bypass it, correctly: `Authorization`/`X-API-Key` are not attached automatically by a browser and so are not forgeable cross-site |
| Rate limiting | 300/min | Keyed by credential first, then address. `X-Forwarded-For` is honoured **only** when `TRUST_PROXY_HEADERS=true`, so a client cannot spoof its identity to evade the limiter |
| Auth rate limit | 10/min | Separate, stricter budget for `/api/auth/login` and `/register` |
| Body size | 25 MiB | `413` before the body is read |
| Error envelope | — | Internal exception text is never returned; clients get a stable code plus a request id |

### Known weakness
**The rate limiter is per-process and in-memory.** With `WEB_CONCURRENCY > 1`
or multiple replicas, each process keeps its own counters, so the effective
limit is multiplied by the number of processes. A shared store (Redis) is
required for correct multi-process limiting.

---

## 5. Script sandbox — read this before enabling script nodes

`app/services/security/sandbox.py`. **Disabled by default.**

### This is defence in depth, NOT a security boundary.

Do not run untrusted code with it. For untrusted code, run the whole backend
inside a container with seccomp, no network and a read-only filesystem, and
treat one execution per container as the isolation unit.

### What it does enforce (verified in `tests/m5/test_sandbox_m5.py`)

Execution moves into a **separate OS process** which constrains itself before
running any user code:

| Limit | Mechanism | Fixes |
| --- | --- | --- |
| CPU | `RLIMIT_CPU` (SIGXCPU, then SIGKILL) | `while True: pass`. **M4 could not stop this** — `asyncio.to_thread` cannot cancel a running thread, so a busy loop pinned a core for the process lifetime. |
| Memory | `RLIMIT_AS` | `[0] * 10**9`, which previously OOM-killed the entire backend. |
| File size | `RLIMIT_FSIZE` | Disk exhaustion. |
| Processes | `RLIMIT_NPROC` | Fork bombs. |
| Core dumps | `RLIMIT_CORE` = 0 | Memory disclosure via dumps. |
| Wall clock | parent timeout + `killpg` | Sleeping processes, which CPU time alone would not catch. |
| Quota | `SCRIPT_EXECUTION_QUOTA_PER_RUN` | A loop node spawning unbounded processes. |

Inside the child: a **PEP 578 audit hook**, an import allowlist, a scrubbed
environment (no `OPENAI_API_KEY`, no `DATABASE_URL`), a private temp working
directory, network denial, and `open`/`input`/`breakpoint` removed from
builtins.

### The audit hook is the actual boundary

The import allowlist alone is **bypassable**, and we verified it:

```python
().__class__.__bases__[0].__subclasses__()   # → BuiltinImporter
BuiltinImporter.load_module("posix")          # → a real os module reference
```

This returns a cached module without re-entering the import machinery, so
neither `builtins.__import__` nor the `import` audit event fires. **This still
works.** `tests/m5/test_sandbox_m5.py::TestDocumentedLimitations` asserts it
does, so this document cannot silently drift out of date.

What makes the recovered reference useless is the audit hook, which runs inside
CPython below any Python-level indirection and **cannot be uninstalled** once
set — not even by code that has recovered the real builtins. Every dangerous
operation raises an audit event and is refused: file open, `os.system`, `exec`,
`spawn`, `fork`, `kill`, `chmod`, `remove`, `rename`, socket use, `ctypes`,
`listdir`, `scandir`, `chdir`, `pickle.find_class`, `marshal.loads`.

Post-escape containment is tested explicitly.

### What it does NOT stop

1. **A CPython interpreter escape.** A memory-corruption bug in the interpreter
   yields the backend user's OS privileges. Resource limits still apply; the
   audit hook may not.
2. **`os.stat` and file metadata.** CPython 3.11 raises no audit event for
   `os.stat`, so a script that has escaped to a module reference can still
   *stat* paths (not read them). Existence and size are disclosable.
3. **The JavaScript node is not sandboxed at all.** It shells out to Node.js
   with the backend user's full permissions, bounded only by a timeout. M5 did
   not change this. Treat `ALLOW_JAVASCRIPT_EXECUTOR=true` as equivalent to
   granting local code execution.
4. **Windows.** `resource` does not exist; the sandbox reports itself
   unavailable and the executor falls back to the weaker in-process path.
5. **Timing and CPU side channels** are not addressed.

### Other flag-gated executors

| Flag | Risk |
| --- | --- |
| `ALLOW_SHELL_EXECUTOR` | Arbitrary local commands. Startup **error** without an allowlist. |
| `ALLOW_DATABASE_EXECUTOR` | Raw SQL against the app database. Stacked statements rejected; writes require `allow_write`. |
| `HTTP_EXECUTOR_ALLOW_PRIVATE_NETWORKS` | SSRF to internal services and cloud metadata. Startup error in production. |

---

## 6. Input handling

- **SQL injection:** all queries use the SQLAlchemy ORM or `text()` with bound
  parameters. The `database` node exposes SQL by design and is flag-gated.
- **XSS:** React escapes by default; there is no `dangerouslySetInnerHTML`
  anywhere in the codebase.
- **Path traversal:** file and folder nodes and all media paths resolve through
  `resolve_media_path`, which rejects absolute paths, `..`, null bytes and
  symlink escapes from `MEDIA_ROOT`.
- **Uploads:** content-based MIME sniffing (not the client's `Content-Type`),
  filename sanitisation, and a size cap.
- **Validation:** every request body is a Pydantic model; failures return a
  structured `422` with no internal detail.

### Known weakness
There is **no antivirus scanning and no archive-bomb protection** on uploads.

---

## 7. Secrets and logging

- Logs pass through a redaction filter covering `sk-*` keys, `api_key`,
  `authorization`, `password` and `token` patterns.
- Secrets are read from the environment. **There is no integration with a
  secret manager**, and no `*_FILE` convention; `.env` files are the documented
  mechanism, which is weaker than a mounted secret.
- `AUTH_SECRET_KEY` must be unique per deployment. Rotating it invalidates
  every issued token. Placeholder values are a startup error.
- The sandbox child receives an environment with credential-shaped variables
  stripped.

---

## 8. Audit logging

`audit_events` records authentication events (login success/failure, password
change, session revocation), user administration and API key lifecycle, with
actor, timestamp and context.

### Known weaknesses
- **Coverage is partial.** Auth and user administration are audited; workflow
  and media mutations are **not** yet.
- `POST /api/enterprise/audit` records the **authenticated principal** as the
  actor and requires `manage_settings`. A caller-supplied `user_id` is retained
  only as `details.subject_user_id`, so the trail cannot be forged. (Before M5
  this endpoint was unauthenticated and took the actor from the request body.)
- Audit rows are **not tamper-evident** — no hash chain or signing. An
  administrator with database access can alter history.

---

## 9. Reporting a vulnerability

Open a private security advisory on the repository, or contact the maintainer
directly. Please do not file a public issue for an exploitable defect. Include
reproduction steps, affected version/commit, and impact.

---

## 10. Security posture summary

| Area | State |
| --- | --- |
| Authentication | Implemented and tested |
| Authorization / RBAC | Implemented and enforced |
| Per-resource authorization | **Not implemented** |
| MFA / SSO | **Not implemented** |
| Transport hardening | Implemented |
| Rate limiting | Implemented, **single-process only** |
| Python sandbox | Hardened; **not a security boundary** |
| JavaScript sandbox | **Not implemented** |
| Audit logging | Partial coverage, not tamper-evident |
| Secret management | Environment only |
| Penetration test | **Never performed** |

Creator OS has not undergone an external security review. The controls
described here were verified by the project's own tests only.
