"""The gateway control plane: every discovery and call request flows through here.

Call pipeline:  resolve → authorize → rate-limit → validate → circuit/concurrency → invoke
               → bound result size → metrics + audit.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import jsonschema
from mcp import types

from .audit import AuditLogger, Timer
from .config import GatewayConfig
from .errors import (
    GatewayError,
    InvalidArguments,
    RateLimited,
    ToolNotFound,
    UpstreamError,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from .metrics import Metrics
from .models import CallContext, Principal, ToolRecord
from .policy import PolicyEngine
from .ratelimit import RateLimiter
from .registry import Registry
from .search import ToolSearchIndex

log = logging.getLogger(__name__)
META_NS = "mini-mcp-gateway"


class Gateway:
    def __init__(
        self, *, config: GatewayConfig, registry: Registry, policy: PolicyEngine, limiter: RateLimiter,
        audit: AuditLogger, metrics: Metrics,
    ) -> None:
        self.config = config
        self.registry = registry
        self.policy = policy
        self.limiter = limiter
        self.audit = audit
        self.metrics = metrics
        self._index: ToolSearchIndex | None = None
        self._index_version = -1
        self._validators: dict[str, tuple[int, Any]] = {}

    # ---- helpers ---------------------------------------------------------------------
    def visible_tools(self, principal: Principal) -> list[ToolRecord]:
        tools = self.policy.filter(principal, self.registry.tools())
        tools.sort(key=lambda t: t.qualified_name)
        return tools

    def _search_index(self) -> ToolSearchIndex:
        if self._index is None or self._index_version != self.registry.version:
            self._index = ToolSearchIndex(self.registry.tools())
            self._index_version = self.registry.version
        return self._index

    def rate_key(self, principal: Principal, client_ip: str | None) -> str:
        """Rate-limit identity: the principal, plus the client IP when credentials are shared."""
        if self.config.limits.per_client_ip and client_ip:
            return f"{principal.id}@{client_ip}"
        return principal.id

    def check_request_rate(self, principal: Principal, client_ip: str | None = None) -> None:
        wait = self.limiter.acquire(f"req:{self.rate_key(principal, client_ip)}", self.config.limits.default_request_rate)
        if wait > 0:
            self.metrics.rate_limited.inc("request")
            raise RateLimited(f"request rate limit exceeded; retry after {wait:.1f}s", retry_after=wait)

    def _audit_base(self, ctx: CallContext) -> dict[str, Any]:
        return {
            "request_id": ctx.request_id, "principal": ctx.principal.id, "roles": list(ctx.principal.roles),
            "transport": ctx.transport, "mode": ctx.discovery_mode, "client_ip": ctx.client_ip,
        }

    # ---- discovery -------------------------------------------------------------------
    def list_tools(self, ctx: CallContext) -> list[ToolRecord]:
        tools = self.visible_tools(ctx.principal)
        self.metrics.discovery.inc("list")
        self.audit.log("tools.list", **self._audit_base(ctx), outcome="ok", result_count=len(tools))
        return tools

    def search(self, ctx: CallContext, query: str, limit: int = 5, server: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), self.config.limits.search_max_results))
        allowed = {t.qualified_name for t in self.visible_tools(ctx.principal)}
        hits = self._search_index().search(query, limit=limit, allowed=allowed, server=server)
        self.metrics.discovery.inc("search")
        self.audit.log(
            "tools.search", **self._audit_base(ctx), outcome="ok", query=query[:200],
            result_count=len(hits), results=[h.tool.qualified_name for h in hits],
        )
        return [
            {
                "name": h.tool.qualified_name,
                "summary": h.tool.summary(),
                "required_args": list((h.tool.input_schema or {}).get("required", [])),
                "read_only": h.tool.read_only,
                "score": h.score,
            }
            for h in hits
        ]

    def describe(self, ctx: CallContext, names: list[str]) -> dict[str, Any]:
        found, missing = [], []
        for name in names[:20]:
            tool = self.registry.get_tool(name)
            if tool is None or not self.policy.is_allowed(ctx.principal, tool):
                missing.append(name)
                continue
            found.append(tool_to_dict(tool))
        self.metrics.discovery.inc("describe")
        self.audit.log(
            "tools.describe", **self._audit_base(ctx), outcome="ok", requested=names[:20], not_found=missing or None,
        )
        out: dict[str, Any] = {"tools": found}
        if missing:
            out["not_found"] = missing
        return out

    # ---- execution -------------------------------------------------------------------
    async def call(self, ctx: CallContext, name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        timer = Timer()
        arguments = arguments or {}
        tool = self.registry.get_tool(name)
        server = tool.server if tool else name.split("__", 1)[0]
        audit = {**self._audit_base(ctx), "tool": name, "server": server}
        # Metric labels must stay bounded: never label with caller-supplied names of unknown tools.
        m_server, m_tool = (tool.server, tool.qualified_name) if tool else ("(unknown)", "(unknown)")
        try:
            result = await self._call(ctx, tool, name, arguments)
        except GatewayError as e:
            outcome = e.code
            self.metrics.tool_calls.inc(m_server, m_tool, outcome)
            self.audit.log(
                "tools.call", **audit, outcome=outcome, error=e.message, error_detail=e.detail,
                latency_ms=timer.ms, arguments=arguments,
            )
            raise
        outcome = "tool_error" if result.isError else "ok"
        self.metrics.tool_calls.inc(m_server, m_tool, outcome)
        self.metrics.tool_latency.observe(timer.ms / 1000, m_server)
        result.meta = {**(result.meta or {}), f"{META_NS}/request_id": ctx.request_id}
        self.audit.log("tools.call", **audit, outcome=outcome, latency_ms=timer.ms, arguments=arguments)
        return result

    async def _call(
        self, ctx: CallContext, tool: ToolRecord | None, name: str, arguments: dict[str, Any],
    ) -> types.CallToolResult:
        if tool is None:
            raise ToolNotFound(f"tool '{name}' not found or not permitted", detail="unknown tool")
        allowed, reason = self.policy.explain(ctx.principal, tool)
        if not allowed:
            raise ToolNotFound(f"tool '{name}' not found or not permitted", detail=reason)

        self._enforce_rate_limits(ctx.principal, tool, ctx.client_ip)
        self._validate(tool, arguments)

        state = self.registry.get_server(tool.server)
        if state is None:
            raise ToolNotFound(f"tool '{name}' not found or not permitted", detail="server removed")
        if not state.breaker.allow():
            self.metrics.circuit_open.set(tool.server, value=1.0)
            raise UpstreamUnavailable(
                f"upstream '{tool.server}' is temporarily unavailable (circuit open); "
                f"retry after {state.breaker.retry_after():.0f}s",
                detail="circuit open",
            )
        timeout = state.spec.timeout_s
        try:
            await asyncio.wait_for(state.semaphore.acquire(), timeout=timeout)
        except TimeoutError:
            raise UpstreamUnavailable(f"upstream '{tool.server}' is overloaded", detail="concurrency limit") from None
        try:
            result = await state.upstream.call_tool(tool.upstream_name, arguments, timeout)
        except (UpstreamUnavailable, UpstreamTimeout, UpstreamError):
            state.breaker.record_failure()
            self.metrics.circuit_open.set(tool.server, value=1.0 if state.breaker.state != "closed" else 0.0)
            raise
        except GatewayError:
            raise
        except Exception as e:  # noqa: BLE001 - never leak raw upstream exceptions to callers
            state.breaker.record_failure()
            log.exception("unexpected error calling %s", name)
            raise UpstreamError(f"upstream '{tool.server}' failed", detail=f"{type(e).__name__}: {e}") from e
        finally:
            state.semaphore.release()
        state.breaker.record_success()
        self.metrics.circuit_open.set(tool.server, value=0.0)
        return self._bound_result(result)

    def _enforce_rate_limits(self, principal: Principal, tool: ToolRecord, client_ip: str | None = None) -> None:
        who = self.rate_key(principal, client_ip)
        spec = self.policy.principal_tool_rate(principal, self.config.limits.default_tool_call_rate)
        wait = self.limiter.acquire(f"call:{who}", spec)
        if wait > 0:
            self.metrics.rate_limited.inc("principal")
            raise RateLimited(f"tool call rate limit exceeded; retry after {wait:.1f}s", retry_after=wait)
        tool_spec = self.policy.tool_specific_rate(principal, tool.qualified_name)
        if tool_spec:
            wait = self.limiter.acquire(f"call:{who}:{tool.qualified_name}", tool_spec)
            if wait > 0:
                self.metrics.rate_limited.inc("tool")
                raise RateLimited(
                    f"rate limit for '{tool.qualified_name}' exceeded; retry after {wait:.1f}s", retry_after=wait
                )

    def _validate(self, tool: ToolRecord, arguments: dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise InvalidArguments("arguments must be a JSON object")
        size = len(json.dumps(arguments, default=str))
        if size > self.config.limits.max_argument_bytes:
            raise InvalidArguments(f"arguments too large ({size} bytes > {self.config.limits.max_argument_bytes})")
        cached = self._validators.get(tool.qualified_name)
        if cached is None or cached[0] != self.registry.version:
            try:
                cls = jsonschema.validators.validator_for(tool.input_schema, default=jsonschema.Draft202012Validator)
                cls.check_schema(tool.input_schema)
                validator = cls(tool.input_schema)
            except jsonschema.SchemaError:
                log.warning("tool %s has an invalid input schema; skipping validation", tool.qualified_name)
                validator = None
            cached = (self.registry.version, validator)
            self._validators[tool.qualified_name] = cached
        validator = cached[1]
        if validator is None:
            return
        errors = sorted(validator.iter_errors(arguments), key=lambda e: list(e.absolute_path))
        if errors:
            msgs = []
            for err in errors[:5]:
                where = "/".join(str(p) for p in err.absolute_path) or "(root)"
                msgs.append(f"{where}: {err.message}")
            raise InvalidArguments("invalid arguments — " + "; ".join(msgs))

    def _bound_result(self, result: types.CallToolResult) -> types.CallToolResult:
        limit = self.config.limits.max_result_bytes
        raw = result.model_dump_json(by_alias=True, exclude_none=True)
        if len(raw.encode("utf-8")) <= limit:
            return result
        text = "".join(c.text for c in result.content if isinstance(c, types.TextContent))
        truncated = text.encode("utf-8")[: max(0, limit - 200)].decode("utf-8", errors="ignore")
        note = f"\n…[truncated by gateway: result exceeded {limit} bytes]"
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=truncated + note)], isError=result.isError,
        )


def tool_to_dict(tool: ToolRecord) -> dict[str, Any]:
    d: dict[str, Any] = {
        "name": tool.qualified_name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }
    if tool.annotations:
        d["annotations"] = tool.annotations
    return d


def to_mcp_tool(tool: ToolRecord) -> types.Tool:
    ann = types.ToolAnnotations(**tool.annotations) if tool.annotations else None
    return types.Tool(
        name=tool.qualified_name, title=tool.title, description=tool.description,
        inputSchema=tool.input_schema, annotations=ann,
    )
