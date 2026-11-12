"""A deliberately unreliable web server for load-testing the checker: random latency, a few errors.

GET /m/<anything>  ->  sleeps uniform(LATENCY_MIN, LATENCY_MAX) seconds (default 0.05-3.0), then answers 200,
                       or with probability ERROR_RATE (default 5%) a 500/503/connection reset.
"""

import asyncio
import os
import random

from aiohttp import web

LATENCY_MIN = float(os.environ.get("LATENCY_MIN", "0.05"))
LATENCY_MAX = float(os.environ.get("LATENCY_MAX", "3.0"))
ERROR_RATE = float(os.environ.get("ERROR_RATE", "0.05"))
rng = random.Random(int(os.environ["SEED"])) if "SEED" in os.environ else random.Random()


async def handle(request: web.Request) -> web.StreamResponse:
    await asyncio.sleep(rng.uniform(LATENCY_MIN, LATENCY_MAX))
    if rng.random() < ERROR_RATE:
        kind = rng.choice(["500", "503", "reset"])
        if kind == "reset":
            request.transport.close()  # an abruptly closed connection
            raise web.HTTPInternalServerError()
        return web.Response(status=int(kind), text="fake failure")
    return web.Response(text="ok")


async def healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/m/{tail:.*}", handle)
    app.router.add_get("/healthz", healthz)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), print=None)
