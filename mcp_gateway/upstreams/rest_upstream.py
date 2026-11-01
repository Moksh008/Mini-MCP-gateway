"""Bridge existing REST backends into MCP tools.

* ``kind: rest`` — operations are declared explicitly in the registry entry.
* ``kind: openapi`` — operations are generated from the backend's OpenAPI 3 document,
  so any service with a spec is MCP-enabled with zero code. Operations can opt out with
  ``x-mcp-exclude: true`` and rename themselves with ``x-mcp-name``.

HTTP 4xx responses are returned to the agent as tool errors (``isError: true``) because they
are meaningful to it ("ride not found"); 5xx, timeouts and connection failures raise gateway
errors so they count toward the circuit breaker.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any
from urllib.parse import quote, urljoin

import httpx
from mcp import types

from ..config import expand_env
from ..errors import ConfigError, UpstreamError, UpstreamTimeout, UpstreamUnavailable
from ..models import RestOperation, RestParam, ServerSpec, ToolDef
from .base import Upstream

log = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")
_PATH_PARAM_RE = re.compile(r"\{([^}]+)\}")
MAX_ERROR_BODY = 2000


def compact_schema(node: Any) -> Any:
    """Drop presentation-only keywords (``title``, ``examples``) that cost tokens but carry no
    information the model needs. Walks schema positions only, so a *property* named
    ``title`` is preserved."""
    if isinstance(node, list):
        return [compact_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k in ("title", "examples", "example"):
            continue
        if k in ("properties", "patternProperties", "$defs", "definitions") and isinstance(v, dict):
            out[k] = {pk: compact_schema(pv) for pk, pv in v.items()}
        elif k in ("items", "additionalProperties", "not", "contains", "anyOf", "oneOf", "allOf", "prefixItems"):
            out[k] = compact_schema(v)
        else:
            out[k] = v
    return out


def build_input_schema(op: RestOperation) -> dict[str, Any]:
    props: dict[str, Any] = {}
    required: list[str] = []
    for p in op.params:
        if p.location == "body":
            continue
        schema = dict(p.schema_)
        if p.description:
            schema.setdefault("description", p.description)
        props[p.name] = schema
        if p.required or p.location == "path":
            required.append(p.name)
    body = op.body_schema
    if body is not None:
        if body.get("type", "object") == "object" and "properties" in body:
            for name, schema in body["properties"].items():
                if name in props:
                    raise ConfigError(f"operation '{op.name}': body property '{name}' collides with a parameter")
                props[name] = schema
            required.extend(r for r in body.get("required", []) if r not in required)
        else:
            props["body"] = body
            required.append("body")
    schema: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    return compact_schema(schema)


class RestUpstream(Upstream):
    def __init__(self, spec: ServerSpec, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(spec)
        self._operations: dict[str, RestOperation] = {}
        self._base_url: str = spec.url or ""
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self.last_error = None

    async def start(self) -> None:
        headers = {k: expand_env(v) for k, v in self.spec.headers.items()}
        self._client = httpx.AsyncClient(
            headers=headers, timeout=self.spec.timeout_s, transport=self._transport,
            limits=httpx.Limits(max_connections=self.spec.max_concurrency),
            follow_redirects=False,
        )
        try:
            await self._load_operations()
            self.last_error = None
        except Exception as e:  # noqa: BLE001 - upstream may be down at boot; retry on refresh
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("upstream %s failed to load operations: %s", self.name, self.last_error)

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()

    async def _load_operations(self) -> None:
        self._operations = {op.name: op for op in self.spec.operations}

    async def list_tools(self) -> list[ToolDef]:
        if not self._operations or self.last_error:
            await self._load_operations()
            self.last_error = None
        tools = []
        for op in self._operations.values():
            read_only = op.read_only if op.read_only is not None else op.method == "GET"
            destructive = op.destructive if op.destructive is not None else op.method == "DELETE"
            tools.append(
                ToolDef(
                    name=op.name,
                    description=op.description or f"{op.method} {op.path}",
                    input_schema=build_input_schema(op),
                    annotations={
                        "readOnlyHint": read_only,
                        "destructiveHint": False if read_only else destructive,
                        "idempotentHint": op.method in ("GET", "PUT", "DELETE"),
                        "openWorldHint": False,
                    },
                )
            )
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout_s: float) -> types.CallToolResult:
        op = self._operations.get(name)
        if op is None or self._client is None:
            raise UpstreamError(f"upstream '{self.name}' has no operation '{name}'")
        args = dict(arguments)
        path = op.path
        query: dict[str, Any] = {}
        headers: dict[str, str] = {}
        for p in op.params:
            if p.name not in args:
                continue
            value = args.pop(p.name)
            if p.location == "path":
                path = path.replace("{" + p.name + "}", quote(str(value), safe=""))
            elif p.location == "query":
                query[p.name] = json.dumps(value) if isinstance(value, dict | list) else value
            elif p.location == "header":
                headers[p.name] = str(value)
        if _PATH_PARAM_RE.search(path):
            raise UpstreamError(f"missing path parameter for '{name}'")
        body: Any = None
        if op.body_schema is not None:
            body = args.pop("body") if "body" in args and "properties" not in op.body_schema else args
        url = urljoin(self._base_url.rstrip("/") + "/", path.lstrip("/"))
        try:
            resp = await self._client.request(
                op.method, url, params=query or None, headers=headers or None,
                json=body if op.method in ("POST", "PUT", "PATCH") else None, timeout=timeout_s,
            )
        except httpx.TimeoutException as e:
            raise UpstreamTimeout(f"upstream '{self.name}' timed out after {timeout_s}s") from e
        except httpx.HTTPError as e:
            raise UpstreamUnavailable(f"upstream '{self.name}' is unavailable", detail=repr(e)) from e
        return self._to_result(resp)

    def _to_result(self, resp: httpx.Response) -> types.CallToolResult:
        if resp.status_code >= 500:
            raise UpstreamError(
                f"upstream '{self.name}' returned HTTP {resp.status_code}", detail=resp.text[:MAX_ERROR_BODY]
            )
        try:
            payload: Any = resp.json() if resp.content else None
        except ValueError:
            payload = None
        if resp.status_code >= 400:
            msg = payload.get("detail", payload) if isinstance(payload, dict) else resp.text[:MAX_ERROR_BODY]
            text = f"HTTP {resp.status_code}: {json.dumps(msg) if not isinstance(msg, str) else msg}"
            return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=True)
        if payload is None:
            text = resp.text or f"HTTP {resp.status_code}"
            return types.CallToolResult(content=[types.TextContent(type="text", text=text)])
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
            structuredContent=payload if isinstance(payload, dict) else None,
        )


class OpenAPIUpstream(RestUpstream):
    METHODS = ("get", "post", "put", "patch", "delete")

    async def _load_operations(self) -> None:
        assert self._client is not None
        spec_url = self.spec.openapi_url or urljoin(self.spec.url.rstrip("/") + "/", "openapi.json")
        resp = await self._client.get(spec_url)
        resp.raise_for_status()
        doc = resp.json()
        if not str(doc.get("openapi", "")).startswith("3"):
            raise ConfigError(f"{spec_url} is not an OpenAPI 3 document")
        if not self.spec.url:
            servers = doc.get("servers") or [{"url": "/"}]
            self._base_url = urljoin(spec_url, servers[0]["url"])
        self._operations = {op.name: op for op in openapi_to_operations(doc, self.spec.include_operations)}
        log.info("upstream %s: generated %d tools from %s", self.name, len(self._operations), spec_url)


def _resolve(doc: dict[str, Any], node: Any, depth: int = 0, seen: tuple[str, ...] = ()) -> Any:
    """Inline local ``$ref``s (cycle-safe, depth-bounded)."""
    if depth > 12:
        return {}
    if isinstance(node, list):
        return [_resolve(doc, n, depth + 1, seen) for n in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        if ref in seen:
            return {"type": "object", "description": f"(recursive {ref.rsplit('/', 1)[-1]})"}
        target: Any = doc
        for part in ref[2:].split("/"):
            target = target.get(part.replace("~1", "/").replace("~0", "~"), {})
        merged = {**copy.deepcopy(target), **{k: v for k, v in node.items() if k != "$ref"}}
        return _resolve(doc, merged, depth + 1, (*seen, ref))
    return {k: _resolve(doc, v, depth + 1, seen) for k, v in node.items()}


def openapi_to_operations(doc: dict[str, Any], include: list[str] | None = None) -> list[RestOperation]:
    ops: list[RestOperation] = []
    for path, item in (doc.get("paths") or {}).items():
        shared_params = item.get("parameters", [])
        for method in OpenAPIUpstream.METHODS:
            op = item.get(method)
            if not op or op.get("x-mcp-exclude"):
                continue
            op_id = op.get("x-mcp-name") or op.get("operationId") or f"{method}_{path}"
            name = _SAFE_NAME_RE.sub("_", op_id).strip("_")[:48]
            if include is not None and name not in include and op.get("operationId") not in include:
                continue
            params: list[RestParam] = []
            for raw in [*shared_params, *op.get("parameters", [])]:
                p = _resolve(doc, raw)
                if p.get("in") not in ("path", "query", "header"):
                    continue
                params.append(
                    RestParam(
                        name=p["name"], location=p["in"], required=bool(p.get("required")),
                        schema=compact_schema(p.get("schema") or {"type": "string"}),
                        description=p.get("description", ""),
                    )
                )
            body_schema = None
            content = (op.get("requestBody") or {}).get("content") or {}
            if "application/json" in content:
                body_schema = compact_schema(_resolve(doc, content["application/json"].get("schema") or {}))
            summary, desc = op.get("summary", ""), op.get("description", "")
            description = summary if not desc else (f"{summary}. {desc}" if summary and summary not in desc else desc)
            ops.append(
                RestOperation(
                    name=name, method=method.upper(), path=path, description=description.strip(),
                    params=params, body_schema=body_schema, tags=op.get("tags", []),
                    read_only=op.get("x-mcp-read-only"), destructive=op.get("x-mcp-destructive"),
                )
            )
    return ops
