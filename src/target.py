"""Resolve upstream target URL from a generic inbound request."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from headers_util import GATEWAY_QUERY_KEYS

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
_PATH_PROXY_RE = re.compile(
    r"^/(?:proxy|forward|p)/(https?://.+)$",
    re.IGNORECASE,
)
_PATH_BARE_URL_RE = re.compile(
    r"^/(https?://.+)$",
    re.IGNORECASE,
)


class TargetError(ValueError):
    """Raised when the forward target cannot be resolved or is rejected."""


def _header(request, *names: str) -> str | None:
    headers = request.headers
    for name in names:
        value = headers.get(name)
        if value:
            return str(value).strip()
    return None


def _env_str(env, key: str, default: str = "") -> str:
    try:
        value = getattr(env, key, None)
    except Exception:
        value = None
    if value is None:
        return default
    return str(value).strip()


def load_route_map(env) -> dict[str, str]:
    raw = _env_str(env, "ROUTE_MAP", "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TargetError(f"ROUTE_MAP is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TargetError("ROUTE_MAP must be a JSON object of name -> base URL")
    return {str(k): str(v).rstrip("/") for k, v in data.items()}


def load_allowed_hosts(env) -> set[str]:
    raw = _env_str(env, "ALLOWED_HOSTS", "")
    if not raw:
        return set()
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _join_base_and_path(base: str, path: str, query: str) -> str:
    base = base.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    # Avoid duplicating trailing slash-only path
    if path == "/":
        path = ""
    suffix = path
    if query:
        # Drop gateway control params from the inbound query before appending.
        kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k not in GATEWAY_QUERY_KEYS]
        if kept:
            suffix = f"{suffix}?{urlencode(kept, doseq=True)}"
    return f"{base}{suffix}"


def _normalize_absolute_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise TargetError("empty target URL")
    if not _SCHEME_RE.match(raw):
        raise TargetError(f"target must be an absolute http(s) URL, got: {raw!r}")
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https"):
        raise TargetError(f"unsupported scheme: {parts.scheme}")
    if not parts.netloc:
        raise TargetError(f"target URL missing host: {raw!r}")
    return raw


def resolve_target(request, env) -> str:
    """
    Resolve upstream URL. Priority:

    1. Header ``X-Forward-To`` / ``X-Target-Url`` (absolute URL)
    2. Query ``__target`` / ``__url`` (absolute URL)
    3. Named route ``X-Route`` / ``__route`` + inbound path/query
    4. Header ``X-Forward-Host`` / query ``__host`` + inbound path/query
    5. Path ``/proxy/<absolute-url>`` or ``/<absolute-url>``

    When using host/route modes, the inbound path & query are appended to the
    upstream base so callers keep a normal REST path on the gateway.
    """
    url = urlsplit(str(request.url))
    path = url.path or "/"
    query = url.query or ""

    # 1) Absolute URL via header
    header_target = _header(request, "X-Forward-To", "X-Target-Url", "X-Target-URL")
    if header_target:
        # Absolute URL in header is the most explicit form — use as-is.
        return _normalize_absolute_url(header_target)

    # 2) Absolute URL via query
    inbound_qs = dict(parse_qsl(query, keep_blank_values=True))
    for key in ("__target", "__url"):
        if key in inbound_qs and inbound_qs[key]:
            return _normalize_absolute_url(inbound_qs[key])

    # 3) Named route
    route_name = _header(request, "X-Route") or inbound_qs.get("__route")
    if route_name:
        routes = load_route_map(env)
        base = routes.get(route_name)
        if not base:
            raise TargetError(f"unknown route: {route_name!r}")
        return _join_base_and_path(base, path, query)

    # 4) Host override
    forward_host = _header(request, "X-Forward-Host") or inbound_qs.get("__host")
    if forward_host:
        host = forward_host.strip()
        if _SCHEME_RE.match(host):
            base = host.rstrip("/")
        else:
            base = f"https://{host.strip('/')}"
        return _join_base_and_path(base, path, query)

    # 5) Path-embedded absolute URL
    for pattern in (_PATH_PROXY_RE, _PATH_BARE_URL_RE):
        match = pattern.match(path)
        if match:
            embedded = match.group(1)
            # Path may have encoded characters; request.url path is already decoded by URL parser
            # but keep query from inbound if embedded URL has none.
            target = _normalize_absolute_url(embedded)
            if query and not urlsplit(target).query:
                kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k not in GATEWAY_QUERY_KEYS]
                if kept:
                    target = f"{target}?{urlencode(kept, doseq=True)}"
            return target

    raise TargetError(
        "missing forward target; provide X-Forward-To, __target, X-Route, "
        "X-Forward-Host, or /proxy/<https://...>"
    )


def assert_host_allowed(target_url: str, env) -> None:
    allowed = load_allowed_hosts(env)
    if not allowed:
        return
    host = urlsplit(target_url).hostname or ""
    if host.lower() not in allowed:
        raise TargetError(f"host not allowed: {host}")
