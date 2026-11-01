"""End-to-end tests: real upstreams (MCP HTTP, MCP stdio, OpenAPI, REST) behind a real gateway."""

from __future__ import annotations

import json

import httpx
import pytest

from .conftest import MCP, auth, free_port

SF = {"pickup_lat": 37.7749, "pickup_lng": -122.4194, "dropoff_lat": 37.6213, "dropoff_lng": -122.379}


def text(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


def payload(result) -> dict:
    return json.loads(text(result))


# ---- health / auth -------------------------------------------------------------------
def test_health_and_readiness(http):
    assert http.get("/healthz").json()["status"] == "ok"
    ready = http.get("/readyz").json()
    assert ready == {"status": "ready", "servers": 5, "tools": 20, "unhealthy_servers": []}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic x"}])
def test_unauthenticated_requests_rejected(http, headers):
    for path in ("/mcp", "/mcp/full", "/v1/tools", "/admin/servers"):
        r = http.post(path, headers=headers, json={}) if path.startswith("/mcp") else http.get(path, headers=headers)
        assert r.status_code == 401, path
        assert r.headers["www-authenticate"].startswith("Bearer")


def test_request_id_propagation(http):
    r = http.get("/v1/whoami", headers={**auth("rider"), "X-Request-ID": "trace-123"})
    assert r.headers["x-request-id"] == "trace-123"
    assert r.json() == {"id": "rider-bot", "roles": ["rider-agent"], "is_admin": False}
    bad = http.get("/v1/whoami", headers={**auth("rider"), "X-Request-ID": "bad id <script>"})
    assert bad.headers["x-request-id"] != "bad id <script>"  # untrusted ids are replaced


# ---- full discovery mode -------------------------------------------------------------
async def test_full_mode_lists_only_permitted_tools(gateway):
    async with MCP(gateway["url"], "admin", full=True) as s:
        admin_tools = {t.name for t in (await s.list_tools()).tools}
    async with MCP(gateway["url"], "rider", full=True) as s:
        rider_tools = {t.name for t in (await s.list_tools()).tools}
    async with MCP(gateway["url"], "analyst", full=True) as s:
        analyst = (await s.list_tools()).tools

    assert len(admin_tools) == 20
    assert {n.split("__")[0] for n in admin_tools} == {"rides", "support", "eats", "payments", "maps"}
    assert "payments__reconcile" not in admin_tools  # x-mcp-exclude honoured
    assert "payments__refund_payment" not in rider_tools and "support__escalate_ticket" not in rider_tools
    assert len(rider_tools) == 18
    assert analyst and all(t.annotations and t.annotations.readOnlyHint for t in analyst)


async def test_full_mode_direct_call(gateway):
    async with MCP(gateway["url"], "rider", full=True) as s:
        r = await s.call_tool("rides__estimate_fare", SF)
        assert not r.isError
        assert payload(r)["fare_usd"] > 0


# ---- incremental discovery mode ------------------------------------------------------
async def test_search_mode_exposes_only_meta_tools(gateway):
    async with MCP(gateway["url"], "rider") as s:
        names = [t.name for t in (await s.list_tools()).tools]
    assert names == ["search_tools", "get_tool_schema", "call_tool"]


async def test_search_describe_call_flow(gateway):
    async with MCP(gateway["url"], "rider") as s:
        hits = payload(await s.call_tool("search_tools", {"query": "book a ride to the airport"}))["results"]
        assert hits[0]["name"] == "rides__request_ride"
        assert "input_schema" not in hits[0]  # compact results, no schemas
        desc = payload(await s.call_tool("get_tool_schema", {"names": ["rides__request_ride", "nope__x"]}))
        assert desc["tools"][0]["input_schema"]["required"][0] == "rider_id"
        assert desc["not_found"] == ["nope__x"]
        r = await s.call_tool("call_tool", {"name": "rides__request_ride", "arguments": {"rider_id": "u1", **SF}})
        ride = payload(r)
        assert ride["status"] == "driver_assigned"
        status = payload(await s.call_tool("call_tool", {"name": "rides__get_ride_status",
                                                         "arguments": {"ride_id": ride["ride_id"]}}))
        assert status["ride_id"] == ride["ride_id"]


async def test_search_never_reveals_forbidden_tools(gateway):
    async with MCP(gateway["url"], "rider") as s:
        hits = payload(await s.call_tool("search_tools", {"query": "refund a payment", "limit": 20}))["results"]
        assert "payments__refund_payment" not in [h["name"] for h in hits]
        desc = payload(await s.call_tool("get_tool_schema", {"names": ["payments__refund_payment"]}))
        assert desc == {"tools": [], "not_found": ["payments__refund_payment"]}


async def test_meta_tool_argument_errors(gateway):
    async with MCP(gateway["url"], "rider") as s:
        r = await s.call_tool("search_tools", {"query": ""})
        assert r.isError and "invalid_arguments" in text(r)
        r = await s.call_tool("call_tool", {"name": "rides__estimate_fare", "arguments": "nope"})
        assert r.isError and "invalid_arguments" in text(r)


# ---- authorization, validation, errors -----------------------------------------------
async def test_forbidden_and_unknown_tools_are_indistinguishable(gateway):
    async with MCP(gateway["url"], "rider") as s:
        forbidden = await s.call_tool("call_tool", {"name": "payments__refund_payment", "arguments": {}})
        unknown = await s.call_tool("call_tool", {"name": "payments__does_not_exist", "arguments": {}})
    assert forbidden.isError and unknown.isError
    assert text(forbidden).replace("refund_payment", "X") == text(unknown).replace("does_not_exist", "X")


async def test_read_only_role_cannot_write(gateway):
    async with MCP(gateway["url"], "analyst") as s:
        ok = await s.call_tool("call_tool", {"name": "rides__estimate_fare", "arguments": SF})
        assert not ok.isError
        denied = await s.call_tool("call_tool", {"name": "rides__request_ride", "arguments": {"rider_id": "u", **SF}})
        assert denied.isError and "tool_not_found" in text(denied)


async def test_schema_validation_before_upstream(gateway):
    async with MCP(gateway["url"], "rider") as s:
        r = await s.call_tool("call_tool", {"name": "maps__geocode", "arguments": {"address": 5, "extra": 1}})
    assert r.isError and "invalid_arguments" in text(r)
    assert "address" in text(r)


async def test_upstream_application_error_is_tool_error(gateway):
    async with MCP(gateway["url"], "rider") as s:
        r = await s.call_tool("call_tool", {"name": "rides__get_ride_status", "arguments": {"ride_id": "missing"}})
        assert r.isError and "not found" in text(r)
        r = await s.call_tool("call_tool", {"name": "maps__geocode", "arguments": {"address": "atlantis"}})
        assert r.isError and "HTTP 404" in text(r)


# ---- every upstream kind -------------------------------------------------------------
async def test_stdio_upstream_order_flow(gateway):
    async with MCP(gateway["url"], "rider") as s:
        menu = payload(await s.call_tool("call_tool", {"name": "eats__get_menu", "arguments": {"restaurant_id": "r1"}}))
        items = [i["item_id"] for i in menu["items"]]
        order = payload(await s.call_tool("call_tool", {"name": "eats__place_order", "arguments": {
            "user_id": "u1", "restaurant_id": "r1", "item_ids": items}}))
        assert order["total_usd"] == 12.5
        tracked = payload(await s.call_tool("call_tool", {"name": "eats__track_order",
                                                          "arguments": {"order_id": order["order_id"]}}))
        assert tracked["status"] == "preparing"


async def test_openapi_upstream_with_injected_credentials(gateway):
    async with MCP(gateway["url"], "support") as s:
        ch = payload(await s.call_tool("call_tool", {"name": "payments__charge_payment", "arguments": {
            "user_id": "u1", "amount_usd": 20, "method_id": "pm_visa_4242"}}))
        assert ch["status"] == "succeeded"
        rf = payload(await s.call_tool("call_tool", {"name": "payments__refund_payment", "arguments": {
            "charge_id": ch["charge_id"], "amount_usd": 5}}))
        assert rf["status"] == "partially_refunded" and rf["refunded_usd"] == 5
        over = await s.call_tool("call_tool", {"name": "payments__refund_payment", "arguments": {
            "charge_id": ch["charge_id"], "amount_usd": 500}})
        assert over.isError and "HTTP 400" in text(over)


async def test_rest_upstream_body_operation(gateway):
    async with MCP(gateway["url"], "rider") as s:
        eta = payload(await s.call_tool("call_tool", {"name": "maps__get_eta", "arguments": {
            "origin_lat": 37.7749, "origin_lng": -122.4194, "dest_lat": 37.6213, "dest_lng": -122.379,
            "mode": "driving"}}))
    assert eta["mode"] == "driving" and eta["eta_min"] > 0


# ---- rate limiting -------------------------------------------------------------------
async def test_per_tool_rate_limit(gateway):
    args = {"user_id": "u1", "amount_usd": 1, "method_id": "pm_wallet"}
    async with MCP(gateway["url"], "rider") as s:
        results = [await s.call_tool("call_tool", {"name": "payments__charge_payment", "arguments": args})
                   for _ in range(3)]
    assert [r.isError for r in results] == [False, False, True]  # burst: 2
    assert "rate_limited" in text(results[2])
    assert results[2].meta["mini-mcp-gateway/error"]["retry_after_s"] > 0


def test_rest_rate_limit_returns_429(http):
    args = {"user_id": "u1", "amount_usd": 1, "method_id": "pm_wallet"}
    codes = [http.post("/v1/tools/payments__charge_payment/call", headers=auth("rider"), json=args).status_code
             for _ in range(3)]
    assert codes[-1] == 429


# ---- REST surface --------------------------------------------------------------------
def test_rest_surface(http):
    r = http.get("/v1/tools/search", params={"q": "track my food delivery"}, headers=auth("rider"))
    assert r.json()["results"][0]["name"] == "eats__track_order"
    assert http.get("/v1/tools/payments__refund_payment", headers=auth("rider")).status_code == 404
    assert http.get("/v1/tools/maps__geocode", headers=auth("rider")).json()["name"] == "maps__geocode"
    r = http.post("/v1/tools/maps__geocode/call", headers=auth("rider"), json={"address": "sfo airport"})
    assert r.status_code == 200 and r.json()["structuredContent"]["lat"] == pytest.approx(37.6213)
    r = http.post("/v1/tools/maps__geocode/call", headers=auth("rider"), json={})
    assert r.status_code == 400 and r.json()["error"] == "invalid_arguments"


# ---- admin API / registry ------------------------------------------------------------
def test_admin_requires_admin_role(http):
    assert http.get("/admin/servers", headers=auth("rider")).status_code == 403
    assert http.get("/admin/servers", headers=auth("admin")).status_code == 200


def test_runtime_register_refresh_disable_deregister(http, gateway):
    maps_port = gateway["backends"]["maps"]
    spec = {
        "name": "geo-v2", "kind": "rest", "url": f"http://127.0.0.1:{maps_port}", "owner": "maps",
        "operations": [{"name": "geocode", "path": "/v1/geocode", "description": "Geocode v2",
                        "params": [{"name": "address", "location": "query", "required": True}]}],
    }
    r = http.post("/admin/servers", headers=auth("admin"), json=spec)
    assert r.status_code == 201, r.text
    assert r.json()["tool_count"] == 1 and r.json()["healthy"]
    assert http.post("/admin/servers", headers=auth("admin"), json=spec).status_code == 409
    assert http.post("/admin/servers", headers=auth("admin"), json=spec, params={"replace": "true"}).status_code == 201

    call = http.post("/v1/tools/geo-v2__geocode/call", headers=auth("admin"), json={"address": "sfo airport"})
    assert call.status_code == 200 and not call.json()["isError"]

    assert http.post("/admin/servers/geo-v2/refresh", headers=auth("admin")).json()["tool_count"] == 1
    assert http.post("/admin/servers/geo-v2/disable", headers=auth("admin")).json()["enabled"] is False
    assert http.get("/v1/tools/geo-v2__geocode", headers=auth("admin")).status_code == 404
    http.post("/admin/servers/geo-v2/enable", headers=auth("admin"))
    assert http.get("/v1/tools/geo-v2__geocode", headers=auth("admin")).status_code == 200

    # persisted to the registry store
    from mcp_gateway.store import RegistryStore
    store = RegistryStore(str(gateway["tmp"] / "registry.db"))
    assert [s.name for s in store.load_all()] == ["geo-v2"]
    store.close()

    assert http.delete("/admin/servers/geo-v2", headers=auth("admin")).status_code == 204
    assert http.get("/v1/tools/geo-v2__geocode", headers=auth("admin")).status_code == 404
    assert http.delete("/admin/servers/geo-v2", headers=auth("admin")).status_code == 404


def test_admin_guards(http):
    stdio = {"name": "evil", "kind": "mcp_stdio", "command": "rm", "args": ["-rf", "/"]}
    assert http.post("/admin/servers", headers=auth("admin"), json=stdio).status_code == 403
    bad_scheme = {"name": "ftp-svc", "kind": "mcp_http", "url": "file:///etc/passwd"}
    assert http.post("/admin/servers", headers=auth("admin"), json=bad_scheme).status_code == 422
    assert http.delete("/admin/servers/rides", headers=auth("admin")).status_code == 409  # config-owned
    assert http.post("/admin/servers", headers=auth("admin"), json={"name": "X!", "kind": "rest"}).status_code == 422


async def test_circuit_breaker_opens_for_dead_upstream(http, gateway):
    dead = {"name": "dead-svc", "kind": "rest", "url": f"http://127.0.0.1:{free_port()}", "timeout_s": 2,
            "operations": [{"name": "ping", "path": "/ping", "description": "Ping"}]}
    assert http.post("/admin/servers", headers=auth("admin"), json=dead).status_code == 201
    try:
        codes = [http.post("/v1/tools/dead-svc__ping/call", headers=auth("admin"), json={}).json()
                 for _ in range(3)]
        # Connection refused surfaces as unavailable (Linux) or a connect timeout (Windows).
        assert all(c["error"] in ("upstream_unavailable", "upstream_timeout") for c in codes[:2])
        assert codes[2]["error"] == "upstream_unavailable" and "circuit open" in codes[2]["message"]
        servers = {s["name"]: s for s in http.get("/admin/servers", headers=auth("admin")).json()["servers"]}
        assert servers["dead-svc"]["circuit"] == "open"
    finally:
        http.delete("/admin/servers/dead-svc", headers=auth("admin"))


# ---- audit and metrics ---------------------------------------------------------------
def test_audit_trail(http, gateway):
    http.post("/v1/tools/support__create_ticket/call", headers=auth("support"), json={
        "user_id": "u1", "subject": "Lost item", "body": "left my phone", "category": "ride"})
    events = http.get("/admin/audit", headers=auth("admin"),
                      params={"principal": "care-copilot", "event": "tools.call"}).json()["events"]
    ev = events[0]
    assert ev["tool"] == "support__create_ticket" and ev["outcome"] == "ok"
    assert ev["latency_ms"] >= 0 and len(ev["args_sha256"]) == 64 and ev["request_id"]
    denied = http.get("/admin/audit", headers=auth("admin"), params={"outcome": "tool_not_found"}).json()["events"]
    assert any(e.get("error_detail", "").startswith(("denied", "no role")) for e in denied)
    assert http.get("/admin/audit", headers=auth("admin"), params={"event": "auth.failure"}).json()["events"]
    log_file = gateway["tmp"] / "audit.jsonl"
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "tools.call" for e in lines)


def test_metric_labels_are_bounded(http):
    for i in range(5):
        http.post(f"/v1/tools/rides__bogus_{i}/call", headers=auth("admin"), json={})
    body = http.get("/metrics").text
    assert "rides__bogus_" not in body
    assert 'tool="(unknown)",outcome="tool_not_found"' in body


def test_admin_stats_and_policy_matrix(http):
    assert http.get("/admin/stats", headers=auth("rider")).status_code == 403
    stats = http.get("/admin/stats", headers=auth("admin")).json()
    assert stats["servers"] == 5 and stats["tools"] >= 20 and isinstance(stats["tool_calls"], list)
    matrix = http.get("/admin/policy/matrix", headers=auth("admin")).json()
    assert {p["id"] for p in matrix["principals"]} == {"ops-admin", "rider-bot", "care-copilot", "bi-analyst"}
    refund = next(r for r in matrix["tools"] if r["tool"] == "payments__refund_payment")
    assert refund["access"]["rider-bot"] == {"allowed": False, "reason": "denied by role 'rider-agent'"}
    assert refund["access"]["care-copilot"]["allowed"] is True


def test_metrics(http):
    body = http.get("/metrics").text
    assert "mcpgw_tool_calls_total" in body and 'outcome="ok"' in body
    assert "mcpgw_registry_tools" in body
    assert "mcpgw_auth_failures_total" in body


async def test_concurrent_calls(gateway):
    import asyncio

    async with httpx.AsyncClient(base_url=gateway["url"], timeout=30) as c:
        rs = await asyncio.gather(*[
            c.post("/v1/tools/maps__reverse_geocode/call", headers=auth("support" if i % 2 else "analyst"),
                   json={"lat": 37.77, "lng": -122.41})
            for i in range(40)
        ])
    ok = [r for r in rs if r.status_code == 200 and not r.json()["isError"]]
    denied = [r for r in rs if r.status_code == 404]
    assert len(ok) == 20 and len(denied) == 20  # support-agent has no maps access; analyst does
