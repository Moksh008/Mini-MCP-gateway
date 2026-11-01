"""Run the four HTTP mock backends (rides, support, payments, maps) in one process.

    python -m examples.run_backends

The ``eats`` backend is stdio-based and is spawned by the gateway itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

import uvicorn

BACKENDS = {
    "rides": ("examples.backends.rides_mcp:app", 9101),
    "support": ("examples.backends.support_mcp:app", 9103),
    "payments": ("examples.backends.payments_api:app", 9104),
    "maps": ("examples.backends.maps_api:app", 9105),
}


def make_servers(host: str = os.environ.get("BACKENDS_HOST", "127.0.0.1")) -> list[uvicorn.Server]:
    return [
        uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on"))
        for app, port in BACKENDS.values()
    ]


async def main() -> None:
    servers = make_servers()
    for name, (_, port) in BACKENDS.items():
        print(f"  {name:<9} http://127.0.0.1:{port}")
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
