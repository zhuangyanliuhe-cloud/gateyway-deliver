"""Cloudflare Worker entrypoint — generic HTTP request gateway."""

from workers import WorkerEntrypoint

from gateway import proxy_request


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await proxy_request(request, self.env)
