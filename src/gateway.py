"""HTTP reverse-proxy core for the long-running ASGI service."""

from __future__ import annotations

import hmac
import ipaddress
import logging
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from headers_util import (
    is_forwardable_request_header,
    is_forwardable_response_header,
    iter_headers,
)
from target import TargetError, assert_host_allowed, resolve_target

logger = logging.getLogger("gateway")

_DEFAULT_MAX_BODY_BYTES = 32 * 1024 * 1024


class BodyTooLarge(Exception):
    """Inbound body exceeded GATEWAY_MAX_BODY_BYTES."""

# Instance metadata and link-local addresses are rejected so an EC2 deploy
# cannot be turned into a hop to the instance metadata service.
_BLOCKED_HOSTS = {
    "metadata.google.internal",
    "metadata.internal",
}
_BLOCKED_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec2::254"),
}


def _env_str(env, key: str, default: str = "") -> str:
    try:
        value = getattr(env, key, None)
    except Exception:
        value = None
    if value is None:
        return default
    return str(value).strip()


def json_error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _header_get(headers, name: str) -> str | None:
    try:
        value = headers.get(name)
    except Exception:
        value = None
    if value is None or value == "":
        return None
    return str(value)


def blocked_target_reason(target_url: str) -> str | None:
    host = (urlsplit(target_url).hostname or "").lower().strip("[]")
    if not host:
        return "target URL missing host"
    if host in _BLOCKED_HOSTS:
        return f"target host is blocked: {host}"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip in _BLOCKED_IPS or ip.is_link_local or ip.is_unspecified:
        return f"target host is blocked: {host}"
    return None


def check_gateway_auth(request: Request, env) -> JSONResponse | None:
    token = _env_str(env, "GATEWAY_TOKEN", "")
    if not token:
        return None

    provided = _header_get(request.headers, "x-gateway-key") or _header_get(
        request.headers, "x-gateway-token"
    )
    if not provided:
        auth = _header_get(request.headers, "authorization") or ""
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()

    if not provided or not hmac.compare_digest(provided, token):
        return json_error("unauthorized", status=401)
    return None


def _collect_forward_request_headers(request: Request, strip_authorization: bool) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in iter_headers(request.headers):
        lower = name.lower()
        if not is_forwardable_request_header(name):
            continue
        if strip_authorization and lower == "authorization":
            continue
        out[name] = value
    return out


def _collect_forward_response_headers(upstream: httpx.Response, origin: str | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    items = getattr(upstream.headers, "multi_items", None)
    pairs = items() if callable(items) else iter_headers(upstream.headers)
    for name, value in pairs:
        if is_forwardable_response_header(name):
            out.append((str(name), str(value)))
    out.append(("X-Gateway", "gateway-deliver"))
    if origin:
        out.append(("Access-Control-Allow-Origin", origin))
        out.append(("Access-Control-Expose-Headers", "*"))
    return out


def _health() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "service": "gateway-deliver",
            "usage": {
                "header": "X-Forward-To: https://upstream.example/path",
                "query": "?__target=https://upstream.example/path",
                "route": "X-Route: <name>  (configure ROUTE_MAP)",
                "host": "X-Forward-Host: upstream.example  (keeps inbound path)",
                "path": "/proxy/https://upstream.example/path",
            },
        }
    )


def _cors_preflight(request: Request) -> Response:
    origin = _header_get(request.headers, "origin") or "*"
    allow_headers = _header_get(request.headers, "access-control-request-headers") or (
        "authorization,content-type,x-forward-to,x-forward-host,x-target-url,"
        "x-route,x-gateway-key"
    )
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET,HEAD,POST,PUT,PATCH,DELETE,OPTIONS",
            "Access-Control-Allow-Headers": allow_headers,
            "Access-Control-Max-Age": "86400",
        },
    )


def _request_path(request: Request) -> str:
    return urlsplit(str(request.url)).path or "/"


async def _read_body(request: Request, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if limit and total > limit:
            raise BodyTooLarge()
        chunks.append(chunk)
    return b"".join(chunks)


async def _open_upstream(
    request: Request,
    client: httpx.AsyncClient,
    target_url: str,
    headers: dict[str, str],
    max_body_bytes: int,
) -> httpx.Response:
    method = request.method.upper()
    # Buffer the inbound body so httpx sends Content-Length. A chunked
    # request body is rejected by many upstream HTTP servers.
    content = None if method in {"GET", "HEAD"} else await _read_body(request, max_body_bytes)
    upstream_req = client.build_request(method, target_url, headers=headers, content=content)
    return await client.send(upstream_req, stream=True)


async def proxy_request(request: Request, env, client: httpx.AsyncClient) -> Response:
    path = _request_path(request)
    method = request.method.upper()

    # Probes and CORS preflight do not carry the gateway token.
    if method == "OPTIONS":
        return _cors_preflight(request)
    if method in {"GET", "HEAD"} and path in {"/health", "/_gateway/health"}:
        return _health()

    auth_err = check_gateway_auth(request, env)
    if auth_err is not None:
        return auth_err

    if method == "GET" and path == "/":
        return _health()

    try:
        target_url = resolve_target(request, env)
        assert_host_allowed(target_url, env)
        blocked = blocked_target_reason(target_url)
        if blocked:
            raise TargetError(blocked)
    except TargetError as exc:
        return json_error(str(exc), status=400)

    strip_auth = bool(_env_str(env, "GATEWAY_TOKEN", ""))
    headers = _collect_forward_request_headers(request, strip_authorization=strip_auth)
    try:
        limit = int(getattr(env, "max_body_bytes", _DEFAULT_MAX_BODY_BYTES))
        upstream = await _open_upstream(request, client, target_url, headers, limit)
    except BodyTooLarge:
        return json_error("request body too large", status=413)
    except Exception as exc:
        logger.warning("upstream fetch failed: %s", exc)
        return json_error(f"upstream fetch failed: {exc}", status=502)

    origin = _header_get(request.headers, "origin")
    response_headers = _collect_forward_response_headers(upstream, origin)

    async def body() -> AsyncIterator[bytes]:
        try:
            # Fully buffered responses (for example a mock transport) already
            # consumed the stream. Live upstreams are read raw so compressed
            # bodies stay intact and Content-Encoding can be forwarded.
            if hasattr(upstream, "_content"):
                if upstream.content:
                    yield upstream.content
                return
            async for chunk in upstream.aiter_raw():
                yield chunk
        except Exception as exc:
            logger.warning("upstream stream failed: %s", exc)
            raise
        finally:
            if not upstream.is_closed:
                await upstream.aclose()

    response = StreamingResponse(body(), status_code=upstream.status_code)
    for name, value in response_headers:
        response.headers.append(name, value)
    return response
