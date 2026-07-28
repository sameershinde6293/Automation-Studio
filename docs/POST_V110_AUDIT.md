# Independent Repository Certification Audit — post-v1.1.0

Repository: `sameershinde6293/Automation-Studio`
Audited tree: `main` @ `0b50be2` (v1.1.0 GA, immediately after PR #12)
Executed: 2026-07-28 · Debian 12, Linux 6.1.158, Python 3.11.2, Node 22, 2 cores

## How this audit was conducted

Every claim was re-verified from scratch. No previous milestone was assumed
correct. **Every Critical/High finding below was reproduced by executing code,
not by reading it**, and each has a regression test in `backend/tests/audit/`
that was confirmed to fail against the pre-fix tree.

Where something could not be executed in this environment it is recorded as
**not executed** — never as verified by proxy.

---

## 1. Result summary

| | |
| --- | --- |
| Total findings | **17** |
| Critical | 1 |
| High | 4 |
| Medium | 5 |
| Low | 4 |
| Informational | 3 |
| **Fixed in this audit** | **5** (all Critical + all High) |
| Left as recommendations | 12 (Medium/Low/Informational) |

The v1.1.0 GA certification reported "no release blockers" and 94% production
readiness. That assessment did not hold: this audit found one Critical and four
High defects in the shipped product, three of them security defects. The M10
report's own findings were all in the documentation and validation layer, which
is consistent with an audit that read the code rather than attacked it.

### Scores

| Dimension | Score | Basis |
| --- | --- | --- |
| Security | **78 / 100** | Strong primitives (pinned-alg JWT, PBKDF2 600k, constant-time compares, fail-closed router-level RBAC, real process sandbox). But a working SSRF bypass, a working rate-limit bypass and an anonymous info-leak all shipped in a GA release that claimed no blockers. Fixed here; the score reflects that they were reachable in a certified build |
| Code quality | **82 / 100** | Genuinely high: no TODO/FIXME, no debug code, no dead code above 80% vulture confidence, honest and specific comments. Deductions for two parallel HTTP node implementations, a 523-line function, and a dead `main.py` stub the README has to warn about |
| Performance | **72 / 100** | Sound design (pool sizing tied to threadpool, bounded SSE queues, `asyncio.to_thread` for blocking DB writes, pool-saturation gauges). **Not re-measured in this audit** — no PostgreSQL and no load run here, so the M10 latency figures are carried forward, not confirmed |
| Documentation | **85 / 100** | Unusually honest and self-critical; zero broken relative links across 30 docs; `KNOWN_ISSUES.md` is a real limitations register. Deducted for published totals that drift from measured reality and for the LICENSE/Dockerfile contradiction |
| Architecture | **84 / 100** | Clean layering that holds under inspection, fail-closed authorization, correct migration discipline, lifespan-owned workers. Ceiling set by acknowledged single-process design (in-memory queue, per-process limiter, per-process SSE) |
| **Overall production readiness** | **79 / 100** | Suitable for single-tenant, trusted-team deployment behind a proxy. Not suitable for untrusted multi-tenant use, which the project itself states |

These are not 100/100 and should not be. Docker has never been executed, CI has
never run, no soak test or multi-replica run exists, and there has been no
external penetration test — all of which the project documents itself.

---

## 2. Phase 1 — Security

### AUDIT-1 · Critical · SSRF guard bypassed by HTTP redirect

**Files:** `app/services/workflow/executors.py`, `app/services/workflow/nodes/network_nodes.py`

`validate_outbound_url()` correctly blocks loopback, private, link-local and
cloud-metadata addresses — but it was applied **only to the initial URL**, and
the client was then constructed with `follow_redirects=True, max_redirects=5`.
Any attacker-controlled public host could answer `302 Location:
http://169.254.169.254/latest/meta-data/` and the request would be made.

Reproduced: the guard refuses a direct request to loopback, then the identical
address is reached through a redirect.

```
GUARD: direct loopback blocked -> SecurityError
REDIRECT FOLLOWED -> 200 'INTERNAL-METADATA-SECRET'
```

Both HTTP node implementations were affected, which is itself a consequence of
finding A-3 (duplicated node).

**Fixed.** Redirects are now followed manually by
`request_following_validated_redirects()`, which runs `validate_outbound_url`
on **every hop**, resolves relative `Location` headers, and applies standard
303/301/302 method rewriting. Post-fix, the same redirect raises `SecurityError`
while a permitted redirect still returns `200 'FINAL-OK'`.

### AUDIT-2 · High · Login rate limiter evaded by rotating a header

**File:** `app/core/middleware.py`

`client_identity()` keys the limiter on the presented credential before falling
back to the address. On `/api/auth/login` the credential *is the thing being
guessed*, so every attempt with a different junk `Authorization` header landed
in its own fresh bucket and the stricter auth budget never fired.

Measured against a 3-per-minute auth budget:

```
no-header codes:        [401, 401, 401, 429, 429, 429]   -> limiter works
rotating-header codes:  [401 x15]                        -> limiter never fires
```

The header is not otherwise used by `/login`, so this costs an attacker nothing.

**Fixed.** `client_identity(..., prefer_address=True)` is now used for
credential endpoints, which bucket by network address. Credential-first keying
is retained everywhere else, where it is the correct choice. Post-fix the
rotating-header run returns `429` on every attempt past the budget.

### AUDIT-3 · High · Anonymous access to deployment and runtime detail

**File:** `app/api/routers/system_router.py`

With `AUTH_ENABLED=true`, four endpoints answered `200` to a caller with no
credential at all, while `/api/workflows/`, `/api/projects/` and
`/api/enterprise/roles` correctly returned `401`:

```
200  /api/system/info            401  /api/workflows/
200  /api/system/metrics         401  /api/projects/
200  /api/system/events          401  /api/enterprise/roles
200  /api/system/scheduler/jobs
```

`/api/system/info` returns the OS build, Python patch version and — most
usefully to an attacker — exactly which risky executors are enabled
(`shell_executor`, `python_executor`, `javascript_executor`,
`database_executor`). `/metrics` returns the PID and RSS. `/events` returns
live workflow and node names with node failure messages.

This was not an oversight in one route: all four were explicitly listed in
`PUBLIC_PATHS` in `tests/m5/test_endpoint_authorization_m5.py`, so the
route-coverage test that exists to catch unprotected endpoints was told to skip
them. The justifying comment ("used by the editor to render its palette")
is true of `/node-types` and `/node-schemas` but not of these four.

**Fixed.** All four now require `read`. `/node-types` and `/node-schemas`
remain public — they are a static description of build capabilities that the
editor needs before a user is known. The test allowlist was narrowed
accordingly, with the reason recorded in place.

### AUDIT-4 · High · Email node leaked bcc recipients and never delivered them

**File:** `app/services/workflow/nodes/data_nodes.py`

Two defects in one line each, with opposite effects:

1. `run()` computed `recipients = to + cc + bcc` and `_send_sync` wrote
   `message["To"] = ", ".join(recipients)` — so **every bcc address was
   printed in the visible `To:` header**, disclosed to all recipients.
2. `smtp.send_message(message)` with no envelope derives recipients from the
   headers. Since a `Bcc:` header is (correctly) never written, **bcc was
   simultaneously never delivered**, while the node returned `sent: true` and
   listed the bcc address in `recipients`.

Verified against a real SMTP server. Before:

```
To header actually sent: to1@x.com, cc1@x.com, secret-bcc@x.com
BCC leaked into visible To header: True
SMTP actually delivers to: ['to1@x.com', 'cc1@x.com']   # bcc dropped
```

After:

```
envelope recipients  : ['cc1@x.com', 'secret@x.com', 'to1@x.com']
visible To header    : to1@x.com
visible Cc header    : cc1@x.com
BCC delivered        : True
BCC leaked in headers: False
```

**Fixed.** Visible headers (`to`/`cc`) and the SMTP envelope (`to + cc + bcc`)
are now separated, and the envelope is passed explicitly to `send_message`.

### AUDIT-5 · High · AI provider ignored its own configuration

**Files:** `app/services/ai/providers/openai_provider.py`, `local_provider.py`

`OpenAIProvider.__init__` read `os.getenv("OPENAI_API_KEY")` directly. Settings
are loaded by pydantic-settings from `.env`, which does **not** populate
`os.environ`. A key configured the documented way was therefore invisible:

```
settings sees key: True
provider sees key: False
```

Every call raised `OPENAI_API_KEY is not set`, and the orchestrator's fallback
chain silently degraded to Ollama or the mock provider — so the failure mode was
"answers are quietly wrong", not "requests fail".

`base_url` was hardcoded in both providers, so `OPENAI_BASE_URL` and
`OLLAMA_BASE_URL` — both documented in `.env.example`,
`.env.production.example` and `INSTALLATION_GUIDE.md` — had no effect at all:

```
setting OPENAI_BASE_URL = https://proxy.internal/v1
provider actually uses  = https://api.openai.com/v1
```

**Fixed.** Both providers resolve credentials and base URL from `settings` at
call time. Verified post-fix that a `.env` key is seen and both base URLs are
honoured.

### Security areas verified as sound (no finding)

| Area | Evidence |
| --- | --- |
| JWT | Algorithm pinned **before** verification (blocks `alg:none` and RS256→HS256 confusion); `hmac.compare_digest`; `exp`/`nbf`/`iss`/`aud`/`typ` all validated, so an access token cannot be replayed as a refresh token; every token carries a `jti` |
| Password hashing | PBKDF2-HMAC-SHA256 at 600,000 iterations (OWASP guidance), per-hash salt, parameters embedded for rehash-on-login, 1 KiB input cap against KDF-cost DoS |
| API keys | 256-bit random, SHA-256 stored (correct — a plain hash is right for high-entropy secrets and must be cheap per request), scopes **intersect** the owner's role so a key can only ever reduce privilege |
| Login | Uniform failure for unknown user vs. wrong password, with a dummy hash verified to equalise timing; account lockout; refresh-token rotation with replay rejection |
| RBAC | Router-level `dependencies=[...]` on all 6 resource routers — fails closed, so a new route is protected on the day it is added. Confirmed by walking the live route table |
| SQL injection | Only one raw-SQL path (`DatabaseNode`, disabled by default), which uses bound parameters via `text()`, rejects stacked statements and gates writes. No string-interpolated SQL anywhere |
| Command injection | No `shell=True`, no `os.system` anywhere. FFmpeg args are `shlex.split` with a blocklist and a path-like rejection; the JS node uses `create_subprocess_exec` with an argv list |
| Path traversal | `resolve_media_path` rejects absolute paths, `..`, null bytes and Windows drive letters, then re-checks the **resolved** path is under `MEDIA_ROOT`, including symlinked parents |
| XSS | No `dangerouslySetInnerHTML`, `innerHTML`, `eval` or `new Function` in the frontend |
| CSRF | Double-submit cookie; correctly exempts header-authenticated requests (those are not browser-forgeable); path canonicalised so `/api/v1` cannot evade exemptions |
| Secrets | No secrets in the tree or git history; `.gitignore` covers `.env*` with explicit template exceptions; log redaction filter on every handler; sandbox children get a scrubbed environment |
| Electron | `nodeIntegration: false`, `contextIsolation: true`, `sandbox: true` |
| Script sandbox | Real separate process with `RLIMIT_CPU/AS/FSIZE/NPROC/CORE`, import allowlist, socket neutralisation, scrubbed env, `-I -S`, `start_new_session` + process-group kill. Limits set post-exec rather than via `preexec_fn`, correctly avoiding the fork-in-threaded-parent deadlock. Documented honestly as **not** a security boundary and disabled by default |

---

## 3. Phase 2 — Code quality

**Verified clean:** zero `TODO`/`FIXME`/`XXX`/`HACK` in `backend/app` or
`frontend/src`; zero `console.log`/`print()`/`breakpoint()`; `vulture` at 80%
confidence reports only one unused import and five false positives (Protocol
method signatures); frontend `tsc --noEmit` clean.

| ID | Sev | Finding | Evidence |
| --- | --- | --- | --- |
| A-2 | Medium | `backend/main.py` is a dead V1.0 stub whose app has no routers | The README must spend a paragraph warning users not to run it. A stub that needs a documented warning is a trap, not a convenience |
| A-3 | Medium | Duplicate HTTP node: `HttpRequestExecutor` (legacy) vs `HTTPRequestNode` | `http_request` resolves to the legacy class, `httpRequest` to the new one. The AUDIT-1 SSRF fix had to be written twice — exactly the maintenance cost duplication predicts |
| A-6 | Medium | `run_execution_v2` is 523 lines; `run_execution` 250; `validate_settings` 249 | Measured by AST walk. The two engine paths duplicate orchestration logic |
| A-5 | Low | `lodash-es` declared as a production dependency, imported nowhere | `grep -rn "lodash" frontend/src` returns nothing |
| A-7 | Low | `backend/test_endpoints.sh:5` sources `venv/bin/activate`; setup creates `.venv/` | Script cannot run as written |
| — | Info | `media_repository.py:3` imports `ConfigDict` unused | vulture, 90% confidence |

**Resource and concurrency handling reviewed, no defect found:** SSE
subscriptions are released in a `finally`; subscriber queues are bounded and
drop oldest rather than stalling the engine; rate-limiter buckets are pruned
above 1024 keys; the sandbox kills the whole process group on timeout and
removes its temp dir in a `finally`; `active_tasks` entries are removed by
`add_done_callback`; the DB engine is disposed on shutdown. The engine's
process-wide `_write_lock` is a known, documented serialisation point.

---

## 4. Phase 3 — Performance

**Not re-measured in this audit** — no PostgreSQL server and no load-test run
in this environment. M10's figures are carried forward and marked as such in
`TEST_COVERAGE.md` and the README rather than restated as fresh.

Design review found the following sound: pool size is deliberately tied to the
ASGI threadpool with an explicit `pool_timeout` so overload sheds fast instead
of hanging; `pool_pre_ping` is on; SQLite uses `StaticPool` appropriately;
blocking DB writes are dispatched with `asyncio.to_thread` so the event loop is
not blocked; workers are owned by the lifespan rather than created per request;
shutdown drains engine, media pipeline, scheduler and DB engine in order, each
independently guarded; pool-saturation gauges exist specifically to distinguish
pool exhaustion from a slow database.

Measured here: backend suite of 1591 tests completes in ~104 s; app import and
73-node registration is sub-second.

---

## 5. Phase 4 — Documentation

**Verified:** all relative links across `README.md`, `CONTRIBUTING.md` and 30
files in `docs/` resolve — zero broken. Version is consistent at `1.1.0` across
`backend/app/version.py`, `frontend/package.json` and `release_notes.txt`.

| ID | Sev | Finding |
| --- | --- | --- |
| A-4 | Medium | No `LICENSE` file, while `backend/Dockerfile:43` declares `org.opencontainers.image.licenses="MIT"`. The published image metadata and the repository's own stated position ("all rights reserved") **contradict each other** |
| — | Low | Published test totals drifted from measured reality (1576 → 1591 after this audit's regression tests). Corrected in `README.md` and `TEST_COVERAGE.md`, with carried-forward figures now explicitly labelled |

The documentation is, on the whole, unusually honest: `KNOWN_ISSUES.md` states
plainly that the sandbox is not a security boundary, that the JS node is not
sandboxed at all, that RBAC has no tenancy, that CI has never run and that
Docker has never been executed. That candour is a genuine strength and the
reason this audit could focus on execution rather than fact-checking prose.

---

## 6. Phase 5 — Dependencies

**Backend runtime: clean.** `pip-audit` reports zero vulnerabilities in any
declared runtime dependency. The only hits are `pip` 23.0.1 and `setuptools`
66.1.1 — build tooling from the base Python image, not shipped by this project.

**Frontend production tree: 1 moderate.** `uuid` <11.1.1
([GHSA-w5hq-g745-h8pq](https://github.com/advisories/GHSA-w5hq-g745-h8pq)) — a
missing bounds check in v3/v5/v6 when `buf` is supplied. Creator OS imports only
`v4`, so it is **not exposed**.

**Frontend dev tree: 30 advisories (3 critical, 24 high)** — `electron-builder`
and its `app-builder-lib`/`@electron/asar`/`tar`/`minimatch` chain, plus
`vitest`/`@vitest/ui`. Build and test tooling only, no runtime exposure.
Recorded as A-1: the fix requires breaking majors and belongs in its own change.

The small, deliberate backend dependency set (10 runtime packages, stdlib
implementations of JWT/PBKDF2/Prometheus in place of C-extension libraries) is a
real supply-chain advantage and is documented with its rationale.

**License compatibility: unresolvable.** Without a LICENSE file the project's
own terms are undefined, so compatibility with its dependencies cannot be
assessed (A-4).

---

## 7. Phase 6 — Architecture

Layering holds under inspection: `app/domain` does not import
`app/infrastructure`; routers stay thin and delegate to services; repositories
mediate all persistence. Lazy router imports keep `app.main` cheap. Migrations
are Alembic-managed and deliberately excluded from container start to avoid a
multi-replica race — a mistake many projects of this size make.

The API is consistently designed: a stable error envelope with request
correlation ids, `/api` served alongside a pinned `/api/v1`, and a
canonicalisation helper so the alias cannot be used to evade middleware
exemptions (CSRF, rate-limit budgets, body-size). That last detail is a class of
bug most codebases ship.

**Ceiling:** the single-process design is the binding constraint —
in-memory execution queue (queued runs lost on restart), per-process rate
limiter (N replicas ⇒ N× the limit), per-process SSE broker. All three are
documented, and all three mean this scales up but not out. Global RBAC with no
per-resource ownership similarly caps it at "one mutually-trusting team per
instance", which the project states.

---

## 8. Verification of this audit's own changes

| Check | Result |
| --- | --- |
| Backend suite, post-fix | **1591 passed, 10 skipped, 0 failed** |
| Frontend suite, post-fix | **179 passed** (13 files) |
| Frontend `tsc --noEmit` | clean |
| New regression tests | 15, in `backend/tests/audit/` |
| New tests fail on pre-fix code | **Confirmed** — reverted `backend/app`, 12 of 15 fail (the other 3 assert behaviour preserved by the fix) |
| Behaviour preserved | Permitted redirects still followed; credential-keyed limiting retained on non-auth endpoints; `/node-types` still public; email without bcc unchanged |

No feature was added and no working code was refactored, per the audit's remit.

---

## 9. What this audit did NOT verify

Stated plainly, because omitting it would be the same failure this audit found
in its predecessors:

- **PostgreSQL** — no server available here. The 1584-test PostgreSQL figure is
  M10's, carried forward, not re-run.
- **Docker** — no container runtime, no registry access. Sixth consecutive
  milestone unverified.
- **CI** — has still never executed on any commit.
- **Load, latency and soak** — no load test run; M10 latency figures carried
  forward, not confirmed.
- **The example workflows** — not re-executed against a live backend here.
- **External penetration testing** — none, at any point in this project.

An SSRF bypass, a rate-limit bypass and an anonymous information leak all
survived a GA certification that reported no blockers. The reasonable inference
is that the areas listed above — none of which have ever been executed — are
more likely to hold defects than the areas that have been.
