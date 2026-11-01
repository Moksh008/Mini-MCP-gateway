"""Upstream abstraction: anything that can list and execute tools."""

from __future__ import annotations

import abc
from typing import Any

from mcp import types

from ..models import ServerSpec, ToolDef


class Upstream(abc.ABC):
    def __init__(self, spec: ServerSpec) -> None:
        self.spec = spec
        self.last_error: str | None = None

    @property
    def name(self) -> str:
        return self.spec.name

    async def start(self) -> None:  # noqa: B027 - optional hook
        """Open connections. Must not raise for transient upstream failures."""

    async def stop(self) -> None:  # noqa: B027 - optional hook
        """Release resources."""

    @property
    def healthy(self) -> bool:
        return self.last_error is None

    @abc.abstractmethod
    async def list_tools(self) -> list[ToolDef]:
        """Return the upstream's tools. May raise on failure."""

    @abc.abstractmethod
    async def call_tool(self, name: str, arguments: dict[str, Any], timeout_s: float) -> types.CallToolResult:
        """Execute a tool. Raise ``UpstreamUnavailable``/``UpstreamTimeout``/``UpstreamError``."""

    def set_tools_changed_callback(self, cb) -> None:  # noqa: B027 - optional hook
        """Upstreams that support change notifications call ``cb()`` when their tools change."""
