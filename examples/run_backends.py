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


async def reset_loop(interval_s: float) -> None:
    """Public demo: periodically wipe the mock backends' in-memory data."""
    from examples.backends import payments_api, rides_mcp, support_mcp

    while True:
        await asyncio.sleep(interval_s)
        for module in (rides_mcp, support_mcp, payments_api):
            module.reset()
        print("demo reset: cleared backend state", flush=True)


async def main() -> None:
    servers = make_servers()
    for name, (_, port) in BACKENDS.items():
        print(f"  {name:<9} http://127.0.0.1:{port}")
    tasks = [s.serve() for s in servers]
    if os.environ.get("DEMO_RESET_S"):
        tasks.append(reset_loop(float(os.environ["DEMO_RESET_S"])))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
