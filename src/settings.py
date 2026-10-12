"""Process configuration loaded from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import httpx


@dataclass(frozen=True)
class Settings:
    GATEWAY_TOKEN: str = ""
    ALLOWED_HOSTS: str = ""
    ROUTE_MAP: str = ""
    timeout: httpx.Timeout | None = None
    max_body_bytes: int = 32 * 1024 * 1024


def _timeout_from_env() -> httpx.Timeout:
    raw = os.environ.get("GATEWAY_UPSTREAM_TIMEOUT", "60").strip()
    if raw.lower() in {"", "0", "none"}:
        return httpx.Timeout(None)
    seconds = float(raw)
    if seconds < 0:
        raise ValueError("GATEWAY_UPSTREAM_TIMEOUT must be >= 0")
    return httpx.Timeout(seconds)


def load_settings() -> Settings:
    route_map = os.environ.get("ROUTE_MAP", "")
    route_file = os.environ.get("ROUTE_MAP_FILE", "").strip()
    if route_file:
        route_map = Path(route_file).read_text(encoding="utf-8")
    raw_limit = os.environ.get("GATEWAY_MAX_BODY_BYTES", str(32 * 1024 * 1024)).strip()
    max_body_bytes = int(raw_limit)
    if max_body_bytes < 0:
        raise ValueError("GATEWAY_MAX_BODY_BYTES must be >= 0")
    return Settings(
        GATEWAY_TOKEN=os.environ.get("GATEWAY_TOKEN", ""),
        ALLOWED_HOSTS=os.environ.get("ALLOWED_HOSTS", ""),
        ROUTE_MAP=route_map,
        timeout=_timeout_from_env(),
        max_body_bytes=max_body_bytes,
    )
