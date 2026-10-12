"""Local unit checks for target URL resolution (no Workers runtime needed)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace


class FakeHeaders(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class FakeRequest:
    def __init__(self, url: str, headers: dict | None = None):
        self.url = url
        self.headers = FakeHeaders(headers or {})


class TargetResolveTests(unittest.TestCase):
    def setUp(self):
        from target import resolve_target, assert_host_allowed, TargetError

        self.resolve_target = resolve_target
        self.assert_host_allowed = assert_host_allowed
        self.TargetError = TargetError
        self.env = SimpleNamespace(ROUTE_MAP="", ALLOWED_HOSTS="")

    def test_header_absolute(self):
        req = FakeRequest(
            "https://gw.example/",
            {"X-Forward-To": "https://httpbin.org/post"},
        )
        self.assertEqual(
            self.resolve_target(req, self.env),
            "https://httpbin.org/post",
        )

    def test_query_target(self):
        req = FakeRequest("https://gw.example/?__target=https://httpbin.org/get")
        self.assertEqual(
            self.resolve_target(req, self.env),
            "https://httpbin.org/get",
        )

    def test_forward_host_keeps_path(self):
        req = FakeRequest(
            "https://gw.example/anything/foo?a=1",
            {"X-Forward-Host": "httpbin.org"},
        )
        self.assertEqual(
            self.resolve_target(req, self.env),
            "https://httpbin.org/anything/foo?a=1",
        )

    def test_named_route(self):
        env = SimpleNamespace(
            ROUTE_MAP='{"demo":"https://httpbin.org"}',
            ALLOWED_HOSTS="",
        )
        req = FakeRequest("https://gw.example/get", {"X-Route": "demo"})
        self.assertEqual(self.resolve_target(req, env), "https://httpbin.org/get")

    def test_path_proxy(self):
        req = FakeRequest("https://gw.example/proxy/https://httpbin.org/get")
        self.assertEqual(
            self.resolve_target(req, self.env),
            "https://httpbin.org/get",
        )

    def test_allowlist(self):
        env = SimpleNamespace(ROUTE_MAP="", ALLOWED_HOSTS="httpbin.org")
        self.assert_host_allowed("https://httpbin.org/x", env)
        with self.assertRaises(self.TargetError):
            self.assert_host_allowed("https://evil.example/x", env)

    def test_missing_target(self):
        req = FakeRequest("https://gw.example/api")
        with self.assertRaises(self.TargetError):
            self.resolve_target(req, self.env)

    def test_rejects_non_http(self):
        req = FakeRequest(
            "https://gw.example/",
            {"X-Forward-To": "ftp://files.example/a"},
        )
        with self.assertRaises(self.TargetError):
            self.resolve_target(req, self.env)


if __name__ == "__main__":
    # Allow `python tests/test_target.py` from repo root by putting src on path.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    unittest.main()
