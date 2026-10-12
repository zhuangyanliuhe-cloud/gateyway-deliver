"""ASGI entrypoint for the long-running gateway process."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route

from gateway import proxy_request
from settings import Settings, load_settings

_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def create_app(
    *,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.settings = settings if settings is not None else load_settings()
        owns_client = client is None
        if owns_client:
            app.state.client = httpx.AsyncClient(
                follow_redirects=False,
                timeout=app.state.settings.timeout,
                trust_env=False,
            )
            # Drop the client User-Agent / Accept defaults so only caller headers are sent.
            app.state.client.headers.clear()
        else:
            app.state.client = client
        try:
            yield
        finally:
            if owns_client:
                await app.state.client.aclose()

    async def endpoint(request: Request):
        return await proxy_request(request, request.app.state.settings, request.app.state.client)

    return Starlette(
        routes=[
            Route("/", endpoint, methods=_METHODS),
            Route("/{path:path}", endpoint, methods=_METHODS),
        ],
        lifespan=lifespan,
    )


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("BIND_PORT", "8787"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
