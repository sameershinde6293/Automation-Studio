"""Network nodes: HTTP request and webhook.

Both reuse the M1 SSRF protection (``validate_outbound_url``) rather than
re-implementing it — that hardening was a critical M1 security fix and must not
be bypassed.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import httpx

from app.core.errors import ValidationError
from app.infrastructure.config.settings import settings
from app.services.workflow.executors import (
    coerce_number,
    render_template,
    render_value,
    validate_outbound_url,
)
from app.services.workflow.runtime import (
    FieldSpec,
    NodeErrorCode,
    NodeExecutionError,
    NodeSchema,
    RuntimeNodeExecutor,
)

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


async def _perform_request(
    *,
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Any,
    timeout: float,
    max_bytes: int,
    follow_redirects: bool = True,
) -> Dict[str, Any]:
    """Shared HTTP call with size caps and typed error classification."""
    request_kwargs: Dict[str, Any] = {"url": url, "headers": headers}
    if method in {"POST", "PUT", "PATCH", "DELETE"} and body is not None:
        if isinstance(body, (dict, list)):
            request_kwargs["json"] = body
        else:
            request_kwargs["content"] = str(body)

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=follow_redirects,
            max_redirects=5,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        ) as client:
            response = await client.request(method, **request_kwargs)
            content = response.content or b""
            truncated = len(content) > max_bytes
            if truncated:
                content = content[:max_bytes]
    except httpx.TimeoutException as exc:
        raise NodeExecutionError(
            f"HTTP request to {url} timed out after {timeout}s.",
            code=NodeErrorCode.TIMEOUT,
            details={"url": url},
        ) from exc
    except httpx.HTTPError as exc:
        raise NodeExecutionError(
            f"HTTP request to {url} failed: {exc}",
            code=NodeErrorCode.NETWORK,
            details={"url": url},
        ) from exc

    content_type = response.headers.get("content-type", "")
    if "json" in content_type and not truncated:
        try:
            data: Any = json.loads(content.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = content.decode("utf-8", errors="replace")
    else:
        data = content.decode("utf-8", errors="replace")

    return {
        "status_code": response.status_code,
        "ok": 200 <= response.status_code < 300,
        "response": data,
        "truncated": truncated,
        "headers": dict(response.headers),
    }


class HTTPRequestNode(RuntimeNodeExecutor):
    """Outbound HTTP request with SSRF protection and a response-size cap."""

    label = "HTTP Request"
    category = "network"
    description = "Performs an outbound HTTP request (SSRF-protected)."
    aliases = ("http_request_node", "http", "request")
    schema = NodeSchema(
        inputs=[
            FieldSpec("url", "string", required=True, description="Target URL"),
            FieldSpec(
                "method", "string", default="GET", enum=sorted(ALLOWED_METHODS)
            ),
            FieldSpec("headers", "object", description="Request headers"),
            FieldSpec("body", "any", description="Request body (JSON or text)"),
            FieldSpec(
                "timeout", "number", minimum=1.0, maximum=300.0, default=30.0
            ),
            FieldSpec(
                "fail_on_error",
                "boolean",
                default=True,
                description="Fail the node on a non-2xx response",
            ),
        ],
        outputs=[
            FieldSpec("status_code", "integer"),
            FieldSpec("ok", "boolean"),
            FieldSpec("response", "any", description="Parsed JSON or text body"),
            FieldSpec("headers", "object"),
        ],
    )

    async def run(self, node, context, config) -> Any:
        url = render_template(str(config.get("url") or ""), context).strip()
        method = str(config.get("method") or "GET").upper()
        if method not in ALLOWED_METHODS:
            raise ValidationError(
                f"HTTP method {method!r} is not supported.",
                details={"method": method, "supported": sorted(ALLOWED_METHODS)},
            )

        validate_outbound_url(url)

        headers = render_value(config.get("headers") or {}, context)
        if not isinstance(headers, dict):
            raise ValidationError("HTTP node 'headers' must be an object.")
        headers = {str(k): str(v) for k, v in headers.items()}

        timeout = coerce_number(
            config.get("timeout"), settings.HTTP_EXECUTOR_TIMEOUT_SECONDS
        )
        timeout = max(1.0, min(timeout, 300.0))

        body = render_value(config.get("body"), context)
        result = await _perform_request(
            method=method,
            url=url,
            headers=headers,
            body=body,
            timeout=timeout,
            max_bytes=settings.HTTP_EXECUTOR_MAX_RESPONSE_BYTES,
        )

        if config.get("fail_on_error", True) and not result["ok"]:
            code = (
                NodeErrorCode.RATE_LIMIT
                if result["status_code"] == 429
                else NodeErrorCode.NETWORK
            )
            raise NodeExecutionError(
                f"HTTP {result['status_code']} from {url}.",
                code=code,
                details={"status_code": result["status_code"], "url": url},
            )
        return result


class WebhookNode(RuntimeNodeExecutor):
    """Posts a JSON payload to an external webhook endpoint.

    Note: this is the *outbound* half. Inbound webhook triggers (an endpoint
    that starts a workflow when called) are not part of M4 — see the roadmap.
    """

    label = "Webhook"
    category = "network"
    description = "Sends a JSON payload to an outbound webhook URL."
    aliases = ("webhook_node", "webhook_send")
    schema = NodeSchema(
        inputs=[
            FieldSpec("url", "string", required=True, description="Webhook URL"),
            FieldSpec("method", "string", default="POST", enum=["POST", "PUT", "PATCH"]),
            FieldSpec("payload", "any", description="JSON payload"),
            FieldSpec("headers", "object"),
            FieldSpec("secret", "string", secret=True,
                      description="Sent as the X-Webhook-Secret header"),
            FieldSpec("timeout", "number", minimum=1.0, maximum=120.0, default=30.0),
            FieldSpec("fail_on_error", "boolean", default=True),
        ],
        outputs=[
            FieldSpec("status_code", "integer"),
            FieldSpec("ok", "boolean"),
            FieldSpec("response", "any"),
        ],
    )

    async def run(self, node, context, config) -> Any:
        url = render_template(str(config.get("url") or ""), context).strip()
        validate_outbound_url(url)

        method = str(config.get("method") or "POST").upper()
        if method not in {"POST", "PUT", "PATCH"}:
            raise ValidationError(
                f"Webhook method {method!r} is not supported.",
                details={"method": method},
            )

        headers = render_value(config.get("headers") or {}, context)
        if not isinstance(headers, dict):
            raise ValidationError("Webhook node 'headers' must be an object.")
        headers = {str(k): str(v) for k, v in headers.items()}
        headers.setdefault("Content-Type", "application/json")
        secret = config.get("secret")
        if secret:
            headers["X-Webhook-Secret"] = str(secret)

        payload = render_value(config.get("payload"), context)
        if payload is None:
            payload = {}

        timeout = coerce_number(config.get("timeout"), 30.0)
        timeout = max(1.0, min(timeout, 120.0))

        result = await _perform_request(
            method=method,
            url=url,
            headers=headers,
            body=payload,
            timeout=timeout,
            max_bytes=settings.HTTP_EXECUTOR_MAX_RESPONSE_BYTES,
        )
        result["delivered"] = result["ok"]

        if config.get("fail_on_error", True) and not result["ok"]:
            raise NodeExecutionError(
                f"Webhook delivery to {url} returned HTTP {result['status_code']}.",
                code=NodeErrorCode.NETWORK,
                details={"status_code": result["status_code"], "url": url},
            )
        return result
