"""In-process upstream backed by Python callables (tests, experiments, embedded tools)."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any

from mcp import types

from ..models import ServerSpec, ToolDef
from .base import Upstream

Handler = Callable[[dict[str, Any]], Any | Awaitable[Any]]


class StaticUpstream(Upstream):
    def __init__(self, name: str, tools: list[tuple[ToolDef, Handler]], description: str = "") -> None:
        # ``rest`` kind with a dummy URL purely to satisfy spec validation; never contacted.
        spec = ServerSpec.model_construct(
            name=name, kind="rest", description=description, owner="", tags=[], enabled=True,
            url="inproc://" + name, headers={}, command=None, args=[], env={}, cwd=None,
            operations=[], openapi_url=None, include_operations=None, timeout_s=30.0, max_concurrency=64,
        )
        super().__init__(spec)
        self._tools = {t.name: (t, h) for t, h in tools}

    async def list_tools(self) -> list[ToolDef]:
        return [t for t, _ in self._tools.values()]

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout_s: float) -> types.CallToolResult:
        _, handler = self._tools[name]
        result = handler(arguments)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, types.CallToolResult):
            return result
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result, default=str))],
            structuredContent=result if isinstance(result, dict) else None,
        )
