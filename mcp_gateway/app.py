"""ASGI application: MCP endpoints, REST API, admin API, health and metrics."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import BaseModel, Field
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .agent import Agent
from .audit import AuditLogger
from .auth import Authenticator
from .config import GatewayConfig
from .core import Gateway, tool_to_dict
from .errors import AuthenticationError, ConfigError, GatewayError, RateLimited
from .mcp_server import build_mcp_server
from .metrics import Metrics
from .models import CallContext, Principal, ServerSpec
from .netguard import UnsafeUpstream, check_upstream_url
from .policy import PolicyEngine
from .ratelimit import InMemoryRateLimiter
from .registry import Registry
from .store import RegistryStore
from .upstreams import Upstream

log = logging.getLogger(__name__)
PUBLIC_PATHS = {"/healthz", "/readyz", "/v1/meta"}
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


# ---------------------------------------------------------------------------------------
# Authentication middleware (pure ASGI so it also covers the mounted MCP endpoints)
# ---------------------------------------------------------------------------------------
class AuthMiddleware:
    def __init__(self, app: ASGIApp, *, get_gateway, authenticator: Authenticator, metrics_public: bool) -> None:
        self.app = app
        self._get_gateway = get_gateway
        self._auth = authenticator
        self._public = PUBLIC_PATHS | ({"/metrics"} if metrics_public else set())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        rid = headers.get("x-request-id", "")
        request_id = rid if _REQUEST_ID_RE.match(rid) else uuid.uuid4().hex
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        async def send_with_rid(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (b"x-request-id", request_id.encode())]
            await send(message)

        if scope["path"] in self._public:
            await self.app(scope, receive, send_with_rid)
            return

        gateway: Gateway = self._get_gateway()
        client = scope.get("client")
        try:
            principal = self._auth.authenticate_header(headers.get("authorization"), headers.get("x-api-key"))
        except AuthenticationError as e:
            gateway.metrics.auth_failures.inc()
            gateway.audit.log("auth.failure", request_id=request_id, outcome="unauthenticated",
                              path=scope["path"], client_ip=client[0] if client else None, error=e.message)
            await _send_json(send_with_rid, 401, {"error": e.code, "message": e.message},
                             [(b"www-authenticate", b'Bearer realm="mini-mcp-gateway"')])
            return
        try:
            gateway.check_request_rate(principal, client[0] if client else None)
        except RateLimited as e:
            gateway.audit.log("request.rate_limited", request_id=request_id, principal=principal.id,
                              outcome="rate_limited", path=scope["path"])
            await _send_json(send_with_rid, 429, {"error": e.code, "message": e.message},
                             [(b"retry-after", str(max(1, int(e.retry_after + 0.999))).encode())])
            return
        state["principal"] = principal
        await self.app(scope, receive, send_with_rid)


async def _send_json(send: Send, status: int, body: dict, extra_headers: list | None = None) -> None:
    payload = json.dumps(body).encode()
    await send({
        "type": "http.response.start", "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()),
                    *(extra_headers or [])],
    })
    await send({"type": "http.response.body", "body": payload})


class _MCPEndpoint:
    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.manager.handle_request(scope, receive, send)


# ---------------------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------------------
def get_gateway(request: Request) -> Gateway:
    return request.app.state.gateway


def get_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(401, "unauthenticated")
    return principal


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_admin:
        raise HTTPException(403, "admin role required")
    return principal


def make_ctx(request: Request, principal: Principal, transport: str = "rest") -> CallContext:
    return CallContext(
        request_id=getattr(request.state, "request_id", uuid.uuid4().hex), principal=principal,
        transport=transport, discovery_mode="rest",
        client_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


# ---------------------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------------------
class AgentRunRequest(BaseModel):
    task: str = Field(min_length=3, max_length=1000)
    mode: str = Field(default="search", pattern="^(search|full)$")
    model: str | None = None


def rest_router(config: GatewayConfig) -> APIRouter:
    """Plain HTTP access for non-MCP clients (same auth, policy, limits and audit)."""
    r = APIRouter(prefix="/v1", tags=["tools"])

    @r.get("/meta")
    async def meta(request: Request):
        """Public: what this deployment is and what it allows (drives the console's banners)."""
        demo = config.demo
        agent = getattr(request.app.state, "agent", None)
        return {
            "version": __version__,
            "public_demo": demo.enabled,
            "demo": {"reset_interval_s": demo.reset_interval_s, "runtime_server_ttl_s": demo.runtime_server_ttl_s,
                     "max_runtime_servers": demo.max_runtime_servers} if demo.enabled else None,
            "agent": agent.info() if agent else {"available": False},
        }

    @r.post("/agent/run")
    async def agent_run(body: AgentRunRequest, request: Request, principal: Principal = Depends(get_principal)):
        """Run the LLM agent as the calling principal. Streams Server-Sent Events."""
        agent: Agent = request.app.state.agent
        ctx = make_ctx(request, principal, transport="agent")
        ctx.discovery_mode = body.mode
        try:
            model = agent.admit(ctx, body.model)
        except GatewayError as e:
            headers = {"Retry-After": str(max(1, int(e.retry_after + 0.999)))} if isinstance(e, RateLimited) else None
            status = e.http_status if e.code != "internal_error" else 503
            return JSONResponse({"error": e.code, "message": e.message}, status_code=status, headers=headers)

        async def events():
            async for event in agent.run(ctx, body.task, body.mode, model):
                yield "data: " + json.dumps(event, default=str) + "\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @r.get("/whoami")
    async def whoami(principal: Principal = Depends(get_principal)):
        return {"id": principal.id, "roles": list(principal.roles), "is_admin": principal.is_admin}

    @r.get("/tools")
    async def list_tools(request: Request, principal: Principal = Depends(get_principal),
                         gw: Gateway = Depends(get_gateway)):
        return {"tools": [tool_to_dict(t) for t in gw.list_tools(make_ctx(request, principal))]}

    @r.get("/tools/search")
    async def search(request: Request, q: str = Query(min_length=1, max_length=500),
                     limit: int = Query(5, ge=1, le=50), server: str | None = None,
                     principal: Principal = Depends(get_principal), gw: Gateway = Depends(get_gateway)):
        return {"results": gw.search(make_ctx(request, principal), q, limit, server)}

    @r.get("/tools/{name}")
    async def describe(name: str, request: Request, principal: Principal = Depends(get_principal),
                       gw: Gateway = Depends(get_gateway)):
        out = gw.describe(make_ctx(request, principal), [name])
        if not out["tools"]:
            raise HTTPException(404, f"tool '{name}' not found or not permitted")
        return out["tools"][0]

    @r.post("/tools/{name}/call")
    async def call(name: str, request: Request, arguments: dict[str, Any] = Body(default_factory=dict),
                   principal: Principal = Depends(get_principal), gw: Gateway = Depends(get_gateway)):
        try:
            result = await gw.call(make_ctx(request, principal), name, arguments)
        except GatewayError as e:
            headers = {"Retry-After": str(max(1, int(e.retry_after + 0.999)))} if isinstance(e, RateLimited) else None
            return JSONResponse({"error": e.code, "message": e.message}, status_code=e.http_status, headers=headers)
        return result.model_dump(by_alias=True, exclude_none=True, mode="json")

    return r


def admin_router(config: GatewayConfig) -> APIRouter:
    r = APIRouter(prefix="/admin", tags=["admin"])

    def audit_admin(gw: Gateway, request: Request, principal: Principal, action: str, **fields) -> None:
        gw.audit.log(f"admin.{action}", request_id=getattr(request.state, "request_id", None),
                     principal=principal.id, outcome="ok", **fields)

    async def check_spec_allowed(spec: ServerSpec, gw: Gateway, replace: bool) -> None:
        if spec.kind == "mcp_stdio" and not config.allow_stdio_registration:
            raise HTTPException(403, "registering mcp_stdio servers via the API is disabled "
                                     "(set allow_stdio_registration: true to enable)")
        for url in filter(None, [spec.url, spec.openapi_url]):
            try:
                await check_upstream_url(url, config.upstream_host_allowlist)
            except UnsafeUpstream as e:
                status = 422 if "http(s)" in str(e) else 403
                raise HTTPException(status, str(e)) from None
        if config.demo.enabled:
            runtime = gw.registry.runtime_servers()
            replacing = replace and any(s.spec.name == spec.name for s in runtime)
            if not replacing and len(runtime) >= config.demo.max_runtime_servers:
                raise HTTPException(429, f"public demo: at most {config.demo.max_runtime_servers} runtime servers "
                                         f"at a time (they expire after {config.demo.runtime_server_ttl_s:.0f}s)")

    @r.get("/servers")
    async def list_servers(_: Principal = Depends(require_admin), gw: Gateway = Depends(get_gateway)):
        return {"servers": [s.to_dict() for s in gw.registry.servers()]}

    @r.get("/servers/{name}")
    async def get_server(name: str, _: Principal = Depends(require_admin), gw: Gateway = Depends(get_gateway)):
        s = gw.registry.get_server(name)
        if not s:
            raise HTTPException(404, "server not found")
        return {**s.to_dict(), "tools": [t.qualified_name for t in s.tools]}

    @r.post("/servers", status_code=201)
    async def register(spec: ServerSpec, request: Request, replace: bool = False,
                       principal: Principal = Depends(require_admin), gw: Gateway = Depends(get_gateway)):
        await check_spec_allowed(spec, gw, replace)
        try:
            state = await gw.registry.register(spec, actor=principal.id, replace=replace)
        except ConfigError as e:
            raise HTTPException(409, e.message) from e
        audit_admin(gw, request, principal, "server.register", server=spec.name, kind=spec.kind)
        return state.to_dict()

    @r.delete("/servers/{name}", status_code=204)
    async def deregister(name: str, request: Request, principal: Principal = Depends(require_admin),
                         gw: Gateway = Depends(get_gateway)):
        try:
            await gw.registry.deregister(name, actor=principal.id)
        except KeyError:
            raise HTTPException(404, "server not found") from None
        except ConfigError as e:
            raise HTTPException(409, e.message) from e
        audit_admin(gw, request, principal, "server.deregister", server=name)

    @r.post("/servers/{name}/refresh")
    async def refresh(name: str, request: Request, principal: Principal = Depends(require_admin),
                      gw: Gateway = Depends(get_gateway)):
        if not gw.registry.get_server(name):
            raise HTTPException(404, "server not found")
        state = await gw.registry.refresh(name)
        audit_admin(gw, request, principal, "server.refresh", server=name)
        return state.to_dict()

    @r.post("/servers/{name}/{action}")
    async def toggle(name: str, action: str, request: Request, principal: Principal = Depends(require_admin),
                     gw: Gateway = Depends(get_gateway)):
        if action not in ("enable", "disable"):
            raise HTTPException(404, "unknown action")
        if not gw.registry.get_server(name):
            raise HTTPException(404, "server not found")
        state = await gw.registry.set_enabled(name, action == "enable")
        audit_admin(gw, request, principal, f"server.{action}", server=name)
        return state.to_dict()

    @r.get("/tools")
    async def catalog(q: str | None = None, server: str | None = None, limit: int = Query(100, ge=1, le=5000),
                      _: Principal = Depends(require_admin), gw: Gateway = Depends(get_gateway)):
        tools = gw.registry.tools()
        if server:
            tools = [t for t in tools if t.server == server]
        if q:
            ql = q.lower()
            tools = [t for t in tools if ql in t.qualified_name.lower() or ql in t.description.lower()]
        tools.sort(key=lambda t: t.qualified_name)
        return {"total": len(tools), "tools": [tool_to_dict(t) for t in tools[:limit]]}

    @r.get("/stats")
    async def stats(_: Principal = Depends(require_admin), gw: Gateway = Depends(get_gateway)):
        servers = gw.registry.servers()
        return {
            **gw.metrics.snapshot(),
            "servers": len(servers),
            "healthy_servers": sum(1 for s in servers if s.to_dict()["healthy"]),
            "tools": len(gw.registry.tools()),
            "registry_version": gw.registry.version,
        }

    @r.get("/policy/matrix")
    async def policy_matrix(_: Principal = Depends(require_admin), gw: Gateway = Depends(get_gateway)):
        """Which configured principal may call which tool, and why."""
        principals = [
            Principal(id=k.id, roles=tuple(k.roles), is_admin=any(config.roles[r].admin for r in k.roles))
            for k in config.api_keys if not k.disabled
        ]
        tools = sorted(gw.registry.tools(), key=lambda t: t.qualified_name)
        rows = []
        for t in tools:
            cells = {}
            for p in principals:
                allowed, reason = gw.policy.explain(p, t)
                cells[p.id] = {"allowed": allowed, "reason": reason}
            rows.append({"tool": t.qualified_name, "server": t.server, "read_only": t.read_only, "access": cells})
        return {"principals": [{"id": p.id, "roles": list(p.roles)} for p in principals], "tools": rows}

    @r.get("/audit")
    async def audit(limit: int = Query(100, ge=1, le=2000), principal: str | None = None,
                    event: str | None = None, tool: str | None = None, outcome: str | None = None,
                    _: Principal = Depends(require_admin), gw: Gateway = Depends(get_gateway)):
        events = gw.audit.recent(limit, principal=principal, event=event, tool=tool, outcome=outcome)
        if config.demo.enabled and config.demo.hide_client_ips:
            events = [{k: v for k, v in e.items() if k != "client_ip"} for e in events]
        return {"events": events}

    return r


async def _demo_janitor(config: GatewayConfig, gw: Gateway) -> None:
    """Public demo housekeeping: expire runtime servers and periodically reset the registry."""
    loop = asyncio.get_running_loop()
    next_reset = loop.time() + config.demo.reset_interval_s
    while True:
        await asyncio.sleep(15)
        try:
            expired = await gw.registry.expire_runtime_servers(config.demo.runtime_server_ttl_s, actor="demo-janitor")
            if expired:
                gw.audit.log("admin.server.expire", principal="demo-janitor", outcome="ok", servers=expired)
            if loop.time() >= next_reset:
                next_reset = loop.time() + config.demo.reset_interval_s
                removed = await gw.registry.reset_to_config(actor="demo-janitor")
                gw.audit.log("admin.demo.reset", principal="demo-janitor", outcome="ok", removed=removed or None)
        except Exception:  # noqa: BLE001 - housekeeping must never die
            log.exception("demo janitor iteration failed")


# ---------------------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------------------
def create_app(config: GatewayConfig, *, embedded_upstreams: list[Upstream] | None = None) -> FastAPI:
    metrics = Metrics()
    authenticator = Authenticator(config.api_keys, config.roles)
    holder: dict[str, Gateway] = {}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        store = RegistryStore(config.registry_db)
        audit = AuditLogger(config.audit)
        registry = Registry(store=store, metrics=metrics, breaker_spec=config.circuit_breaker)
        gateway = Gateway(config=config, registry=registry, policy=PolicyEngine(config.roles),
                          limiter=InMemoryRateLimiter(), audit=audit, metrics=metrics)
        holder["gw"] = gateway
        app.state.gateway = gateway
        app.state.agent = Agent(config.agent, gateway)
        managers = {
            mode: StreamableHTTPSessionManager(app=build_mcp_server(gateway, mode), stateless=True, json_response=True)
            for mode in ("search", "full")
        }
        app.state.mcp_endpoints["search"].manager = managers["search"]
        app.state.mcp_endpoints["full"].manager = managers["full"]
        async with contextlib.AsyncExitStack() as stack:
            for m in managers.values():
                await stack.enter_async_context(m.run())
            await registry.bootstrap(config.servers)
            for up in embedded_upstreams or []:
                await registry.register_upstream(up)
            registry.start_background_refresh(config.refresh_interval_s)
            janitor = asyncio.create_task(_demo_janitor(config, gateway)) if config.demo.enabled else None
            app.state.ready = True
            log.info("gateway ready: %d servers, %d tools", len(registry.servers()), len(registry.tools()))
            try:
                yield
            finally:
                app.state.ready = False
                if janitor:
                    janitor.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await janitor
                await registry.close()
                audit.close()
                store.close()

    app = FastAPI(title="Mini MCP Gateway", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ready = False
    app.state.mcp_endpoints = {"search": _MCPEndpoint(None), "full": _MCPEndpoint(None)}  # type: ignore[arg-type]

    methods = ["GET", "POST", "DELETE"]
    app.router.routes.append(Route("/mcp", endpoint=app.state.mcp_endpoints["search"], methods=methods))
    app.router.routes.append(Route("/mcp/full", endpoint=app.state.mcp_endpoints["full"], methods=methods))
    app.include_router(rest_router(config))
    app.include_router(admin_router(config))

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request):
        if not request.app.state.ready:
            return JSONResponse({"status": "starting"}, status_code=503)
        gw: Gateway = request.app.state.gateway
        servers = gw.registry.servers()
        unhealthy = [s.spec.name for s in servers if not s.to_dict()["healthy"]]
        return {"status": "ready", "servers": len(servers), "tools": len(gw.registry.tools()),
                "unhealthy_servers": unhealthy}

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(request: Request):
        if not config.metrics_public:
            require_admin(get_principal(request))
        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(_: Request, e: GatewayError):
        return JSONResponse({"error": e.code, "message": e.message}, status_code=e.http_status)

    app.add_middleware(AuthMiddleware, get_gateway=lambda: holder["gw"], authenticator=authenticator,
                       metrics_public=config.metrics_public)
    return app
