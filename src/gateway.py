"""Generic HTTP reverse-proxy core for Cloudflare Python Workers."""

from __future__ import annotations

from urllib.parse import urlsplit

from workers import Request, Response, fetch

from headers_util import (
    is_forwardable_request_header,
    is_forwardable_response_header,
    iter_headers,
)
from target import TargetError, assert_host_allowed, resolve_target


def _env_str(env, key: str, default: str = "") -> str:
    try:
        value = getattr(env, key, None)
    except Exception:
        value = None
    if value is None:
        return default
    return str(value).strip()


def json_error(message: str, status: int = 400) -> Response:
    return Response.json({"ok": False, "error": message}, status=status)


def _header_get(headers, name: str) -> str | None:
    """Read a header from either JS Headers or a Python mapping."""
    try:
        value = headers.get(name)
    except Exception:
        value = None
    if value is None:
        try:
            value = headers.get(name.lower())
        except Exception:
            value = None
    if value is None or value == "":
        return None
    return str(value)


def check_gateway_auth(request, env) -> Response | None:
    token = _env_str(env, "GATEWAY_TOKEN", "")
    if not token:
        return None

    headers = request.headers
    provided = _header_get(headers, "X-Gateway-Key") or _header_get(
        headers, "X-Gateway-Token"
    )
    if not provided:
        auth = _header_get(headers, "Authorization") or ""
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()

    if not provided or provided != token:
        return json_error("unauthorized", status=401)
    return None


def _collect_forward_request_headers(request, strip_authorization: bool) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in iter_headers(request.headers):
        lower = name.lower()
        if not is_forwardable_request_header(name):
            continue
        if strip_authorization and lower == "authorization":
            continue
        # Last-wins for combined maps; multi-value headers are rare for auth/content-type.
        out[name] = value
    return out


def _collect_forward_response_headers(upstream, origin: str | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for name, value in iter_headers(upstream.headers):
        if is_forwardable_response_header(name):
            out.append((name, value))
    out.append(("X-Gateway", "gateway-deliver"))
    if origin:
        out.append(("Access-Control-Allow-Origin", origin))
        out.append(("Access-Control-Expose-Headers", "*"))
    return out


def _cors_preflight(request) -> Response:
    origin = _header_get(request.headers, "Origin") or "*"
    allow_headers = _header_get(request.headers, "Access-Control-Request-Headers") or (
        "authorization,content-type,x-forward-to,x-forward-host,x-target-url,"
        "x-route,x-gateway-key"
    )
    return Response(
        None,
        status=204,
        headers={
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET,HEAD,POST,PUT,PATCH,DELETE,OPTIONS",
            "Access-Control-Allow-Headers": allow_headers,
            "Access-Control-Max-Age": "86400",
        },
    )


def _request_path(request) -> str:
    return urlsplit(str(request.url)).path or "/"


async def build_upstream_request(request, target_url: str, env) -> Request:
    strip_auth = bool(_env_str(env, "GATEWAY_TOKEN", ""))
    headers = _collect_forward_request_headers(request, strip_authorization=strip_auth)
    method = str(getattr(request, "method", "GET"))
    if hasattr(method, "value"):
        method = method.value
    method = method.upper()

    kwargs: dict = {
        "method": method,
        "headers": headers,
        "redirect": "manual",
    }
    if method not in ("GET", "HEAD"):
        body = getattr(request, "body", None)
        if body is not None:
            kwargs["body"] = body
            # Required by the Fetch spec when streaming a request body.
            kwargs["duplex"] = "half"

    return Request(target_url, **kwargs)


async def proxy_request(request, env) -> Response:
    auth_err = check_gateway_auth(request, env)
    if auth_err is not None:
        return auth_err

    url_path = _request_path(request)
    method = str(getattr(request, "method", "GET"))
    if hasattr(method, "value"):
        method = method.value
    method = method.upper()

    if method == "GET" and url_path in ("/", "/health", "/_gateway/health"):
        return Response.json(
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

    if method == "OPTIONS":
        return _cors_preflight(request)

    try:
        target_url = resolve_target(request, env)
        assert_host_allowed(target_url, env)
    except TargetError as exc:
        return json_error(str(exc), status=400)

    try:
        upstream_req = await build_upstream_request(request, target_url, env)
        upstream = await fetch(upstream_req)
    except Exception as exc:
        return json_error(f"upstream fetch failed: {exc}", status=502)

    origin = _header_get(request.headers, "Origin")
    return Response(
        upstream.body,
        status=int(upstream.status),
        status_text=str(getattr(upstream, "status_text", "") or ""),
        headers=_collect_forward_response_headers(upstream, origin),
    )
