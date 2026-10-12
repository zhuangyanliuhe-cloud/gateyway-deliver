"""Hop-by-hop and proxy-control header helpers."""

from __future__ import annotations

# RFC 7230 hop-by-hop headers — must not be forwarded.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",  # fetch recalculates from body
}

# Headers consumed by this gateway; never forward upstream.
GATEWAY_HEADERS = {
    "x-forward-to",
    "x-forward-host",
    "x-target-url",
    "x-route",
    "x-gateway-key",
    "x-gateway-token",
}

# Query params consumed by this gateway.
GATEWAY_QUERY_KEYS = {
    "__target",
    "__url",
    "__route",
    "__host",
}


def is_forwardable_request_header(name: str) -> bool:
    lower = name.lower()
    if lower in HOP_BY_HOP or lower in GATEWAY_HEADERS:
        return False
    # Strip Authorization only when it is used as gateway auth —
    # handled separately by the caller when GATEWAY_TOKEN is set.
    return True


def is_forwardable_response_header(name: str) -> bool:
    return name.lower() not in HOP_BY_HOP


def iter_headers(headers):
    """Yield (name, value) pairs from JS Headers or a Python mapping.

    Cloudflare/Pyodide Headers often iterate as *names* (strings). Unpacking
    those as ``for name, value in headers`` raises ValueError ("too many
    values to unpack") because a string unpacks to characters.
    """
    # Prefer JS Headers.entries() when present.
    try:
        iterator = headers.entries()
        while True:
            nxt = iterator.next()
            if getattr(nxt, "done", False):
                break
            pair = nxt.value
            yield str(pair[0]), str(pair[1])
        return
    except Exception:
        pass

    # Mapping / Headers.keys()
    try:
        for name in headers.keys():
            value = headers.get(name)
            if value is None or value == "":
                continue
            yield str(name), str(value)
        return
    except Exception:
        pass

    # Fallback: iterable of pairs, or iterable of names.
    try:
        for item in headers:
            if isinstance(item, str):
                try:
                    value = headers.get(item)
                except Exception:
                    continue
                if value is None or value == "":
                    continue
                yield item, str(value)
                continue
            try:
                name, value = item[0], item[1]
            except Exception:
                continue
            yield str(name), str(value)
    except Exception:
        return
