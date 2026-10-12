"""ASGI gateway tests with a mocked upstream client."""

from __future__ import annotations

import sys
import unittest
import warnings
from pathlib import Path

import httpx

warnings.filterwarnings(
    "ignore",
    message=r"Using `httpx` with `starlette.testclient` is deprecated.*",
)
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app import create_app
from settings import Settings


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, parts: list[bytes]):
        self._parts = parts
        self.closed = False

    async def __aiter__(self):
        for part in self._parts:
            yield part

    async def aclose(self):
        self.closed = True


class GatewayAppTests(unittest.TestCase):
    def setUp(self):
        self.seen: dict = {}

        def handle(request: httpx.Request) -> httpx.Response:
            self.seen = {
                "method": request.method,
                "url": str(request.url),
                "body": request.content,
                "headers": {k.lower(): v for k, v in request.headers.items()},
            }
            return httpx.Response(
                201,
                content=b"upstream-body",
                headers={"X-Upstream": "yes", "Content-Type": "text/plain"},
            )

        self.upstream = httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
            follow_redirects=False,
        )
        self.upstream.headers.clear()
        self.settings = Settings(
            GATEWAY_TOKEN="secret",
            ALLOWED_HOSTS="httpbin.org",
            ROUTE_MAP='{"demo":"https://httpbin.org"}',
        )
        self.app = create_app(settings=self.settings, client=self.upstream)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.client = None

    def test_health_skips_auth(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(self.seen, {})

    def test_proxy_requires_token(self):
        response = self.client.post(
            "/",
            headers={"X-Forward-To": "https://httpbin.org/post"},
            content=b"{}",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "unauthorized")

    def test_forwards_body_and_strips_gateway_headers(self):
        response = self.client.post(
            "/",
            headers={
                "Authorization": "Bearer secret",
                "X-Forward-To": "https://httpbin.org/post",
                "X-Gateway-Key": "secret",
                "Content-Type": "application/json",
                "X-Custom": "keep",
                "User-Agent": "caller/1",
            },
            content=b'{"hello":"world"}',
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.content, b"upstream-body")
        self.assertEqual(response.headers["x-upstream"], "yes")
        self.assertEqual(response.headers["x-gateway"], "gateway-deliver")
        self.assertEqual(self.seen["method"], "POST")
        self.assertEqual(self.seen["url"], "https://httpbin.org/post")
        self.assertEqual(self.seen["body"], b'{"hello":"world"}')
        self.assertNotIn("authorization", self.seen["headers"])
        self.assertNotIn("x-forward-to", self.seen["headers"])
        self.assertNotIn("x-gateway-key", self.seen["headers"])
        self.assertEqual(self.seen["headers"].get("content-type"), "application/json")
        self.assertEqual(self.seen["headers"].get("x-custom"), "keep")
        self.assertEqual(self.seen["headers"].get("user-agent"), "caller/1")

    def test_named_route_keeps_path(self):
        response = self.client.get(
            "/get?a=1",
            headers={"Authorization": "Bearer secret", "X-Route": "demo"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.seen["url"], "https://httpbin.org/get?a=1")

    def test_rejects_host_outside_allowlist(self):
        response = self.client.post(
            "/",
            headers={
                "Authorization": "Bearer secret",
                "X-Forward-To": "https://evil.example/x",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("host not allowed", response.json()["error"])
        self.assertEqual(self.seen, {})

    def test_rejects_instance_metadata(self):
        open_settings = Settings(GATEWAY_TOKEN="", ALLOWED_HOSTS="", ROUTE_MAP="")
        with TestClient(create_app(settings=open_settings, client=self.upstream)) as client:
            response = client.post(
                "/",
                headers={"X-Forward-To": "http://169.254.169.254/latest/meta-data"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("blocked", response.json()["error"])
        self.assertEqual(self.seen, {})

    def test_streams_upstream_chunks(self):
        chunks = _Chunks([b"hello ", b"world"])

        class Transport(httpx.AsyncBaseTransport):
            def __init__(self):
                self.seen = None

            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                await request.aread()
                self.seen = request.content
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/plain", "X-Upstream": "stream"},
                    stream=chunks,
                )

        transport = Transport()
        upstream = httpx.AsyncClient(transport=transport, follow_redirects=False)
        upstream.headers.clear()
        app = create_app(
            settings=Settings(GATEWAY_TOKEN="secret", ALLOWED_HOSTS="httpbin.org", ROUTE_MAP=""),
            client=upstream,
        )
        with TestClient(app) as client:
            response = client.post(
                "/",
                headers={
                    "Authorization": "Bearer secret",
                    "X-Forward-To": "https://httpbin.org/post",
                },
                content=b"abc",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"hello world")
        self.assertEqual(response.headers["x-upstream"], "stream")
        self.assertEqual(response.headers["content-type"], "text/plain")
        self.assertEqual(transport.seen, b"abc")
        self.assertTrue(chunks.closed)

    def test_rejects_oversized_body(self):
        app = create_app(
            settings=Settings(
                GATEWAY_TOKEN="",
                ALLOWED_HOSTS="httpbin.org",
                ROUTE_MAP="",
                max_body_bytes=4,
            ),
            client=self.upstream,
        )
        with TestClient(app) as client:
            response = client.post(
                "/",
                headers={"X-Forward-To": "https://httpbin.org/post"},
                content=b"12345",
            )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"], "request body too large")
        self.assertEqual(self.seen, {})

    def test_options_preflight_skips_auth(self):
        response = self.client.options(
            "/",
            headers={"Origin": "https://app.example", "Access-Control-Request-Headers": "x-forward-to"},
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["access-control-allow-origin"], "https://app.example")


if __name__ == "__main__":
    unittest.main()
