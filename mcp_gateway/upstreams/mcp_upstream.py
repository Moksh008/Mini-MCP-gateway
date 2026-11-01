"""Upstream MCP servers over Streamable HTTP or stdio.

A supervisor task owns the client session for its entire lifetime (anyio cancel scopes must
be entered and exited by the same task). It reconnects with exponential backoff when the
connection breaks, which is detected by failed calls or by a periodic ping.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession, McpError, types
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from ..config import expand_env
from ..errors import UpstreamError, UpstreamTimeout, UpstreamUnavailable
from ..models import ServerSpec, ToolDef
from .base import Upstream

log = logging.getLogger(__name__)

PING_INTERVAL_S = 30.0
MAX_BACKOFF_S = 30.0


def tool_from_mcp(t: types.Tool) -> ToolDef:
    return ToolDef(
        name=t.name,
        description=t.description or "",
        input_schema=t.inputSchema or {"type": "object", "properties": {}},
        output_schema=t.outputSchema,
        annotations=t.annotations.model_dump(exclude_none=True) if t.annotations else None,
        title=t.title,
    )


class MCPUpstream(Upstream):
    def __init__(self, spec: ServerSpec, connect_timeout_s: float = 10.0) -> None:
        super().__init__(spec)
        self._session: ClientSession | None = None
        self._connected = asyncio.Event()
        self._broken = asyncio.Event()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._connect_timeout_s = connect_timeout_s
        self._tools_changed_cb = None
        self.last_error = "not connected"

    # ---- lifecycle -------------------------------------------------------------------
    async def start(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name=f"mcp-upstream:{self.name}")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._connected.wait(), timeout=self._connect_timeout_s)

    async def stop(self) -> None:
        self._stopping.set()
        self._broken.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            except Exception:  # pragma: no cover - best effort shutdown
                log.exception("error stopping upstream %s", self.name)

    def set_tools_changed_callback(self, cb) -> None:
        self._tools_changed_cb = cb

    @property
    def healthy(self) -> bool:
        return self._session is not None and self.last_error is None

    def _transport(self):
        spec = self.spec
        if spec.kind == "mcp_http":
            headers = {k: expand_env(v) for k, v in spec.headers.items()}
            return streamablehttp_client(spec.url, headers=headers, timeout=spec.timeout_s)
        command = sys.executable if spec.command in ("python", "python3") else spec.command
        env = {**get_default_environment(), **{k: expand_env(v) for k, v in spec.env.items()}}
        return stdio_client(StdioServerParameters(command=command, args=spec.args, env=env, cwd=spec.cwd))

    async def _on_message(self, message) -> None:
        if isinstance(message, types.ServerNotification) and isinstance(
            message.root, types.ToolListChangedNotification
        ):
            log.info("upstream %s reported tools/list_changed", self.name)
            if self._tools_changed_cb:
                self._tools_changed_cb(self.name)
        elif isinstance(message, Exception):
            log.warning("upstream %s transport error: %r", self.name, message)

    async def _supervise(self) -> None:
        backoff = 0.5
        while not self._stopping.is_set():
            self._broken.clear()
            try:
                async with self._transport() as streams:
                    read, write = streams[0], streams[1]
                    async with ClientSession(
                        read, write, message_handler=self._on_message,
                        read_timeout_seconds=timedelta(seconds=self.spec.timeout_s),
                    ) as session:
                        await asyncio.wait_for(session.initialize(), timeout=self._connect_timeout_s)
                        self._session, self.last_error, backoff = session, None, 0.5
                        self._connected.set()
                        log.info("connected to upstream %s (%s)", self.name, self.spec.kind)
                        await self._hold(session)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - includes ExceptionGroup from task groups
                self.last_error = _describe(e)
                log.warning("upstream %s connection failed: %s", self.name, self.last_error)
            finally:
                self._session = None
                self._connected.clear()
            if self._stopping.is_set():
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def _hold(self, session: ClientSession) -> None:
        """Keep the session open until stop/broken, pinging periodically."""
        while not self._broken.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._broken.wait(), timeout=PING_INTERVAL_S)
            if self._broken.is_set():
                return
            try:
                await asyncio.wait_for(session.send_ping(), timeout=min(10.0, self.spec.timeout_s))
            except Exception as e:  # noqa: BLE001
                self.last_error = f"ping failed: {_describe(e)}"
                return

    async def _get_session(self, timeout_s: float) -> ClientSession:
        if self._session is None:
            try:
                await asyncio.wait_for(self._connected.wait(), timeout=min(2.0, timeout_s))
            except TimeoutError:
                raise UpstreamUnavailable(
                    f"upstream '{self.name}' is unavailable", detail=self.last_error
                ) from None
        assert self._session is not None
        return self._session

    # ---- operations ------------------------------------------------------------------
    async def list_tools(self) -> list[ToolDef]:
        session = await self._get_session(self.spec.timeout_s)
        tools: list[ToolDef] = []
        cursor: str | None = None
        try:
            for _ in range(100):  # hard page cap
                result = await session.list_tools(
                    params=types.PaginatedRequestParams(cursor=cursor) if cursor else None
                )
                tools.extend(tool_from_mcp(t) for t in result.tools)
                cursor = result.nextCursor
                if not cursor:
                    break
        except McpError as e:
            raise UpstreamError(f"upstream '{self.name}' failed to list tools", detail=e.error.message) from e
        except Exception as e:
            self._broken.set()
            raise UpstreamUnavailable(f"upstream '{self.name}' is unavailable", detail=_describe(e)) from e
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout_s: float) -> types.CallToolResult:
        session = await self._get_session(timeout_s)
        try:
            return await asyncio.wait_for(
                session.call_tool(name, arguments, read_timeout_seconds=timedelta(seconds=timeout_s)),
                timeout=timeout_s + 1.0,
            )
        except TimeoutError as e:
            raise UpstreamTimeout(f"upstream '{self.name}' timed out after {timeout_s}s") from e
        except McpError as e:
            if e.error.code == httpx.codes.REQUEST_TIMEOUT:
                raise UpstreamTimeout(f"upstream '{self.name}' timed out after {timeout_s}s") from e
            raise UpstreamError(f"upstream '{self.name}' returned an error: {e.error.message}") from e
        except (UpstreamTimeout, UpstreamError):
            raise
        except Exception as e:
            self._broken.set()
            raise UpstreamUnavailable(f"upstream '{self.name}' is unavailable", detail=_describe(e)) from e


def _describe(e: BaseException) -> str:
    if isinstance(e, BaseExceptionGroup):
        return "; ".join(_describe(x) for x in e.exceptions)
    return f"{type(e).__name__}: {e}"
