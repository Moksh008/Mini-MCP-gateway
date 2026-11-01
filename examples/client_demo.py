"""End-to-end demo: an MCP client using the gateway's incremental discovery flow.

    python -m examples.client_demo [--url http://127.0.0.1:8787] [--key demo-rider-key]
"""

from __future__ import annotations

import argparse
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _text(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


async def run(url: str, key: str) -> None:
    headers = {"Authorization": f"Bearer {key}"}

    async with streamablehttp_client(f"{url}/mcp/full", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            full = await s.list_tools()
            print(f"[full mode]        tools/list -> {len(full.tools)} tool definitions")

    async with streamablehttp_client(f"{url}/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            tools = await s.list_tools()
            print(f"[incremental mode] tools/list -> {[t.name for t in tools.tools]}")
            print(f"  server instructions: {init.instructions[:90]}…")

            res = await s.call_tool("search_tools", {"query": "how much would a ride to the airport cost", "limit": 3})
            hits = json.loads(_text(res))["results"]
            print("  search_tools ->", [h["name"] for h in hits])

            res = await s.call_tool("get_tool_schema", {"names": [hits[0]["name"]]})
            schema = json.loads(_text(res))["tools"][0]
            print(f"  get_tool_schema -> {schema['name']} requires {schema['input_schema'].get('required')}")

            args = {"pickup_lat": 37.7749, "pickup_lng": -122.4194, "dropoff_lat": 37.6213, "dropoff_lng": -122.379}
            res = await s.call_tool("call_tool", {"name": schema["name"], "arguments": args})
            print(f"  call_tool -> isError={res.isError} {_text(res)}")

            res = await s.call_tool("call_tool", {"name": "payments__refund_payment", "arguments": {"charge_id": "x"}})
            print(f"  call_tool (forbidden) -> isError={res.isError} {_text(res)}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8787")
    p.add_argument("--key", default="demo-rider-key")
    a = p.parse_args()
    asyncio.run(run(a.url, a.key))


if __name__ == "__main__":
    main()
