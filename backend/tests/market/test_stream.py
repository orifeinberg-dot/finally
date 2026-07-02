"""Integration tests for the SSE streaming endpoint (stream.py).

Both `httpx.ASGITransport` and Starlette's `TestClient` buffer the entire
ASGI response before returning it to the caller, so neither can exercise a
genuinely infinite SSE stream (`_generate_events`'s `while True` loop never
produces a final "more_body: False" message, so those transports just hang
waiting for the app coroutine to finish). To get real, non-buffered
streaming semantics -- including real client-disconnect detection -- these
tests run the app on an actual uvicorn server bound to a loopback socket and
talk to it with a real `httpx.AsyncClient` over the network.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from app.market.cache import PriceCache
from app.market.stream import create_stream_router


@asynccontextmanager
async def _running_app(cache: PriceCache, interval: float = 0.05) -> AsyncIterator[int]:
    """Run the streaming app on a real loopback socket; yields the bound port."""
    app = FastAPI()
    app.include_router(create_stream_router(cache, interval=interval))
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield port
    finally:
        server.should_exit = True
        await task


async def _next_matching_line(line_iter, prefix: str) -> str:
    async for line in line_iter:
        if line.startswith(prefix):
            return line
    raise AssertionError(f"stream ended before a line starting with {prefix!r} was seen")


@pytest.mark.asyncio
class TestStreamPrices:
    """Integration tests exercising the real wire contract of /api/stream/prices."""

    async def test_retry_preamble(self):
        """The very first thing sent is the SSE retry directive."""
        cache = PriceCache()
        async with _running_app(cache) as port:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                async with client.stream("GET", "/api/stream/prices") as response:
                    assert response.status_code == 200
                    first_line = await anext(response.aiter_lines())
                    assert first_line == "retry: 1000"

    async def test_response_headers(self):
        """Streaming response uses the correct media type and no-cache headers."""
        cache = PriceCache()
        async with _running_app(cache) as port:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                async with client.stream("GET", "/api/stream/prices") as response:
                    assert response.headers["content-type"].startswith("text/event-stream")
                    assert response.headers["cache-control"] == "no-cache"
                    assert response.headers["connection"] == "keep-alive"

    async def test_emits_data_frame_with_expected_shape(self):
        """A cache update produces a `data:` frame with the expected JSON payload."""
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        async with _running_app(cache) as port:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                async with client.stream("GET", "/api/stream/prices") as response:
                    line_iter = response.aiter_lines()
                    data_line = await asyncio.wait_for(
                        _next_matching_line(line_iter, "data:"), timeout=5.0
                    )

        payload = json.loads(data_line[len("data:") :].strip())
        assert set(payload.keys()) == {"AAPL"}
        assert payload["AAPL"]["ticker"] == "AAPL"
        assert payload["AAPL"]["price"] == 190.50
        assert payload["AAPL"]["direction"] == "flat"

    async def test_no_frame_when_cache_unchanged(self):
        """No second `data:` frame is emitted while the cache version is stable."""
        cache = PriceCache()
        cache.update("AAPL", 100.0)
        async with _running_app(cache, interval=0.05) as port:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                async with client.stream("GET", "/api/stream/prices") as response:
                    line_iter = response.aiter_lines()
                    await asyncio.wait_for(_next_matching_line(line_iter, "data:"), timeout=5.0)

                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(
                            _next_matching_line(line_iter, "data:"), timeout=0.3
                        )

    async def test_second_frame_after_new_update(self):
        """A subsequent cache update produces a second, distinct `data:` frame."""
        cache = PriceCache()
        cache.update("AAPL", 100.0)
        async with _running_app(cache, interval=0.05) as port:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                async with client.stream("GET", "/api/stream/prices") as response:
                    line_iter = response.aiter_lines()
                    first = await asyncio.wait_for(
                        _next_matching_line(line_iter, "data:"), timeout=5.0
                    )

                    cache.update("AAPL", 101.0)
                    second = await asyncio.wait_for(
                        _next_matching_line(line_iter, "data:"), timeout=5.0
                    )

        assert first != second
        second_payload = json.loads(second[len("data:") :].strip())
        assert second_payload["AAPL"]["price"] == 101.0
        assert second_payload["AAPL"]["direction"] == "up"


class TestCreateStreamRouter:
    """Tests for the router-creation footgun fixed in stream.py (§4.4)."""

    def test_repeated_calls_return_independent_routers(self):
        """Each call to create_stream_router() returns its own APIRouter,
        so calling it more than once never double-registers a route."""
        cache = PriceCache()
        router1 = create_stream_router(cache)
        router2 = create_stream_router(cache)

        assert router1 is not router2
        assert len(router1.routes) == 1
        assert len(router2.routes) == 1
