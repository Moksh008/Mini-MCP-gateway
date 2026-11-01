"""MCP endpoints exposed by the gateway.

Two discovery modes, each on its own Streamable HTTP endpoint:

``/mcp/full``  — classic: ``tools/list`` returns every tool the caller is permitted to use.
                 Simple, but every tool definition is loaded into the model's context.

``/mcp``       — incremental (default): ``tools/list`` returns three small meta-tools
                 (``search_tools``, ``get_tool_schema``, ``call_tool``). The agent searches
                 for what it needs, fetches only those schemas, then calls through the
                 gateway. Context cost is O(tools used), not O(tools available).

Both endpoints run in stateless mode, so gateway replicas can be load-balanced freely.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server

from . import __version__
from .core import Gateway, to_mcp_tool
from .errors import GatewayError, InvalidArguments, RateLimited
from .models import CallContext, Principal

PAGE_SIZE = 500

SEARCH_INSTRUCTIONS = (
    "This gateway fronts a large catalog of internal tools. They are not listed up front. "
    "To use one: (1) call search_tools with a short natural-language description of the action; "
    "(2) call get_tool_schema for the most relevant result(s) to see exact parameters; "
    "(3) call call_tool with the tool name and arguments. Search again with different words if "
    "nothing relevant comes back."
)

META_TOOLS = [
    types.Tool(
        name="search_tools",
        description=(
            "Search the catalog of available tools by what you want to do. Returns the best "
            "matches as name + one-line summary (no schemas)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you want to do, e.g. 'refund a payment'"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    ),
    types.Tool(
        name="get_tool_schema",
        description="Get the full description and JSON input schema of tools returned by search_tools.",
        inputSchema={
            "type": "object",
            "properties": {
                "names": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10},
            },
            "required": ["names"],
            "additionalProperties": False,
        },
        annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    ),
    types.Tool(
        name="call_tool",
        description="Invoke a tool by name. 'arguments' must match the tool's input schema.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "arguments": {"type": "object"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    ),
]


class _GatewayServer(Server):
    """Authorization and validation are done by the gateway per caller, so disable the SDK's
    per-server tool cache lookup (it would re-run tools/list on every call)."""

    async def _get_cached_tool_definition(self, tool_name: str):  # type: ignore[override]
        return None


def _json_result(payload: Any) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structuredContent=payload if isinstance(payload, dict) else None,
    )


def error_result(e: GatewayError) -> types.CallToolResult:
    meta: dict[str, Any] = {"code": e.code}
    if isinstance(e, RateLimited):
        meta["retry_after_s"] = round(e.retry_after, 2)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"[{e.code}] {e.message}")],
        isError=True,
        _meta={"mini-mcp-gateway/error": meta},
    )


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(base64.urlsafe_b64decode(cursor.encode()).decode()))
    except Exception:  # noqa: BLE001
        return 0


def build_mcp_server(gateway: Gateway, mode: str) -> Server:
    assert mode in ("full", "search")
    server = _GatewayServer(
        "mini-mcp-gateway", version=__version__,
        instructions=SEARCH_INSTRUCTIONS if mode == "search" else None,
    )

    def ctx() -> CallContext:
        request = server.request_context.request
        state = request.state if request is not None else None
        principal: Principal | None = getattr(state, "principal", None)
        if principal is None:  # defensive: the auth middleware guarantees this
            raise RuntimeError("unauthenticated MCP request reached the gateway")
        return CallContext(
            request_id=getattr(state, "request_id", None) or uuid.uuid4().hex,
            principal=principal, transport="mcp", discovery_mode=mode,
            client_ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )

    @server.list_tools()
    async def list_tools(req: types.ListToolsRequest) -> types.ListToolsResult:
        c = ctx()
        if mode == "search":
            gateway.metrics.discovery.inc("list_meta")
            gateway.audit.log("tools.list", request_id=c.request_id, principal=c.principal.id,
                              mode=mode, outcome="ok", result_count=len(META_TOOLS))
            return types.ListToolsResult(tools=META_TOOLS)
        tools = gateway.list_tools(c)
        offset = _decode_cursor(req.params.cursor if req.params else None)
        page = tools[offset: offset + PAGE_SIZE]
        nxt = _encode_cursor(offset + PAGE_SIZE) if offset + PAGE_SIZE < len(tools) else None
        return types.ListToolsResult(tools=[to_mcp_tool(t) for t in page], nextCursor=nxt)

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        c = ctx()
        try:
            if mode == "search":
                if name == "search_tools":
                    query = arguments.get("query")
                    if not isinstance(query, str) or not query.strip():
                        raise InvalidArguments("'query' must be a non-empty string")
                    results = gateway.search(c, query, int(arguments.get("limit", 5) or 5))
                    return _json_result({"results": results, "hint": "Use get_tool_schema before call_tool."})
                if name == "get_tool_schema":
                    names = arguments.get("names")
                    if not isinstance(names, list) or not all(isinstance(n, str) for n in names) or not names:
                        raise InvalidArguments("'names' must be a non-empty array of strings")
                    return _json_result(gateway.describe(c, names))
                if name == "call_tool":
                    target = arguments.get("name")
                    if not isinstance(target, str):
                        raise InvalidArguments("'name' must be a string")
                    args = arguments.get("arguments") or {}
                    if not isinstance(args, dict):
                        raise InvalidArguments("'arguments' must be an object")
                    return await gateway.call(c, target, args)
            # full mode, or a direct call by qualified name in search mode
            return await gateway.call(c, name, arguments)
        except GatewayError as e:
            return error_result(e)

    return server
