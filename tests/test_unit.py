"""Unit tests for the gateway's building blocks (no network)."""

from __future__ import annotations

import json

import pytest
from mcp import types

from mcp_gateway.audit import AuditLogger
from mcp_gateway.auth import Authenticator, generate_api_key, hash_key
from mcp_gateway.config import (
    ApiKeySpec,
    AuditSpec,
    CircuitBreakerSpec,
    GatewayConfig,
    RateLimitSpec,
    RoleSpec,
    expand_env,
)
from mcp_gateway.core import Gateway
from mcp_gateway.errors import AuthenticationError, ConfigError
from mcp_gateway.metrics import Metrics
from mcp_gateway.models import Principal, RestOperation, RestParam, ServerSpec, ToolRecord, qualify
from mcp_gateway.policy import PolicyEngine
from mcp_gateway.ratelimit import InMemoryRateLimiter
from mcp_gateway.registry import CircuitBreaker
from mcp_gateway.search import ToolSearchIndex, tokenize
from mcp_gateway.upstreams.rest_upstream import build_input_schema, compact_schema, openapi_to_operations


def tool(name: str, desc: str = "", read_only: bool = False, props: dict | None = None) -> ToolRecord:
    server, _, short = name.partition("__")
    return ToolRecord(
        qualified_name=name, server=server, upstream_name=short, description=desc,
        input_schema={"type": "object", "properties": props or {}},
        annotations={"readOnlyHint": read_only},
    )


# ---- naming --------------------------------------------------------------------------
def test_qualify_sanitizes_and_bounds():
    assert qualify("rides", "estimate_fare") == "rides__estimate_fare"
    assert qualify("maps", "get.route/v2") == "maps__get_route_v2"
    assert len(qualify("x" * 30, "y" * 100)) == 64


def test_server_name_validation():
    with pytest.raises(ValueError):
        ServerSpec(name="Bad_Name", kind="mcp_http", url="http://x")
    with pytest.raises(ValueError):
        ServerSpec(name="ok", kind="mcp_http")  # missing url
    with pytest.raises(ValueError):
        ServerSpec(name="ok", kind="mcp_stdio")  # missing command


# ---- policy --------------------------------------------------------------------------
ROLES = {
    "rider": RoleSpec(allow=["rides__*", "payments__*"], deny=["payments__refund"]),
    "reader": RoleSpec(allow=["*"], allow_writes=False),
    "ops": RoleSpec(admin=True, deny=["danger__*"]),
}


def test_policy_allow_deny_precedence():
    p = PolicyEngine(ROLES)
    rider = Principal("u", ("rider",))
    assert p.is_allowed(rider, tool("rides__request"))
    assert p.is_allowed(rider, tool("payments__charge"))
    assert not p.is_allowed(rider, tool("payments__refund"))
    assert not p.is_allowed(rider, tool("support__create"))


def test_policy_deny_wins_across_roles():
    p = PolicyEngine(ROLES)
    both = Principal("u", ("reader", "rider"))
    assert not p.is_allowed(both, tool("payments__refund", read_only=True))


def test_policy_read_only_role():
    p = PolicyEngine(ROLES)
    reader = Principal("u", ("reader",))
    assert p.is_allowed(reader, tool("rides__status", read_only=True))
    assert not p.is_allowed(reader, tool("rides__request", read_only=False))


def test_policy_admin_and_unknown_role():
    p = PolicyEngine(ROLES)
    assert p.is_allowed(Principal("a", ("ops",)), tool("anything__x"))
    assert not p.is_allowed(Principal("a", ("ops",)), tool("danger__x"))
    assert not p.is_allowed(Principal("n", ("nope",)), tool("rides__x"))


def test_policy_rate_resolution():
    roles = {
        "a": RoleSpec(allow=["*"], rate_limit=RateLimitSpec(per_minute=10),
                      tool_rate_limits={"pay__*": RateLimitSpec(per_minute=2)}),
        "b": RoleSpec(allow=["*"], rate_limit=RateLimitSpec(per_minute=50)),
    }
    p = PolicyEngine(roles)
    pr = Principal("x", ("a", "b"))
    assert p.principal_tool_rate(pr, RateLimitSpec(per_minute=1)).per_minute == 50
    assert p.tool_specific_rate(pr, "pay__charge").per_minute == 2
    assert p.tool_specific_rate(pr, "rides__x") is None


# ---- rate limiting -------------------------------------------------------------------
def test_token_bucket_burst_and_refill():
    now = [0.0]
    rl = InMemoryRateLimiter(clock=lambda: now[0])
    spec = RateLimitSpec(per_minute=60, burst=3)  # 1 token/s
    assert [rl.acquire("k", spec) for _ in range(3)] == [0, 0, 0]
    wait = rl.acquire("k", spec)
    assert wait == pytest.approx(1.0)
    now[0] = 1.0
    assert rl.acquire("k", spec) == 0
    assert rl.acquire("other", spec) == 0  # keys are independent


def test_rate_limiter_evicts_lru():
    rl = InMemoryRateLimiter(max_keys=2)
    spec = RateLimitSpec(per_minute=60, burst=1)
    for k in ("a", "b", "c"):
        rl.acquire(k, spec)
    assert "a" not in rl._buckets and len(rl._buckets) == 2


# ---- auth ----------------------------------------------------------------------------
def test_authenticator():
    key, digest = generate_api_key()
    assert key.startswith("mgw_") and digest == hash_key(key)
    auth = Authenticator(
        [ApiKeySpec(id="svc", key_sha256=digest, roles=["ops"]),
         ApiKeySpec(id="off", key_sha256=hash_key("disabled"), roles=["rider"], disabled=True)],
        ROLES,
    )
    p = auth.authenticate_header(f"Bearer {key}")
    assert p.id == "svc" and p.is_admin
    assert auth.authenticate_header(None, x_api_key=key).id == "svc"
    for bad in [None, "Bearer wrong", "Basic abc", "Bearer "]:
        with pytest.raises(AuthenticationError):
            auth.authenticate_header(bad)
    with pytest.raises(AuthenticationError):
        auth.authenticate_header("Bearer disabled")


# ---- config --------------------------------------------------------------------------
def test_expand_env(monkeypatch):
    monkeypatch.setenv("TOK", "s3cret")
    assert expand_env("Bearer ${TOK}") == "Bearer s3cret"
    assert expand_env("${MISSING_X:-fallback}") == "fallback"
    with pytest.raises(ConfigError):
        expand_env("${MISSING_X}")


def test_config_rejects_unknown_role_and_duplicates():
    with pytest.raises(ConfigError):
        GatewayConfig(api_keys=[ApiKeySpec(id="a", key_sha256="0" * 64, roles=["ghost"])])
    s = {"name": "dup", "kind": "mcp_http", "url": "http://x"}
    with pytest.raises(ConfigError):
        GatewayConfig.model_validate({"servers": [s, s]})


# ---- audit ---------------------------------------------------------------------------
def test_audit_redacts_and_truncates(tmp_path):
    a = AuditLogger(AuditSpec(path=str(tmp_path / "a.jsonl"), max_value_chars=10))
    args = {"card_number": "4242424242424242", "nested": {"api_key": "k", "note": "x" * 50}, "ok": 1}
    rec = a.log("tools.call", arguments=args, principal="p", outcome="ok")
    assert rec["args"]["card_number"] == "[REDACTED]"
    assert rec["args"]["nested"]["api_key"] == "[REDACTED]"
    assert rec["args"]["nested"]["note"].startswith("xxxxxxxxxx…")
    assert rec["args"]["ok"] == 1
    assert rec["args_sha256"] == AuditLogger.fingerprint(args)
    a.close()
    line = json.loads((tmp_path / "a.jsonl").read_text(encoding="utf-8").strip())
    assert line["args"]["card_number"] == "[REDACTED]" and "4242" not in json.dumps(line)
    assert a.recent(10, principal="p")[0]["event"] == "tools.call"
    assert a.recent(10, principal="other") == []


# ---- circuit breaker -----------------------------------------------------------------
def test_circuit_breaker_transitions():
    now = [0.0]
    cb = CircuitBreaker(CircuitBreakerSpec(failure_threshold=2, reset_after_s=10), clock=lambda: now[0])
    assert cb.allow()
    cb.record_failure()
    assert cb.state == "closed"
    cb.record_failure()
    assert cb.state == "open" and not cb.allow()
    now[0] = 10
    assert cb.state == "half_open"
    assert cb.allow() and not cb.allow()  # exactly one trial request
    cb.record_failure()
    assert cb.state == "open"
    now[0] = 20
    assert cb.allow()
    cb.record_success()
    assert cb.state == "closed" and cb.allow()


# ---- search --------------------------------------------------------------------------
CATALOG = [
    tool("rides__estimate_fare", "Estimate the price of a ride between two coordinates before booking.", True),
    tool("rides__request_ride", "Book a ride for a rider from pickup to drop-off."),
    tool("payments__refund_payment", "Refund a charge fully or partially."),
    tool("payments__get_invoice", "Get the invoice / receipt for a charge.", True),
    tool("maps__geocode", "Convert a street address into latitude/longitude coordinates.", True),
    tool("support__create_ticket", "Open a customer support ticket for a complaint or issue."),
]


@pytest.mark.parametrize("query,expected", [
    ("how much does a trip cost", "rides__estimate_fare"),
    ("book a car to the airport", "rides__request_ride"),
    ("refund the customer", "payments__refund_payment"),
    ("receipt for my charge", "payments__get_invoice"),
    ("coordinates for an address", "maps__geocode"),
    ("file a complaint", "support__create_ticket"),
    ("payments__get_invoice", "payments__get_invoice"),
])
def test_search_top1(query, expected):
    idx = ToolSearchIndex(CATALOG)
    hits = idx.search(query, limit=3)
    assert hits and hits[0].tool.qualified_name == expected, [h.tool.qualified_name for h in hits]


def test_search_respects_allowed_set_and_server():
    idx = ToolSearchIndex(CATALOG)
    hits = idx.search("refund charge", allowed={"payments__get_invoice"})
    assert [h.tool.qualified_name for h in hits] == ["payments__get_invoice"]
    assert all(h.tool.server == "maps" for h in idx.search("address coordinates ride", server="maps"))


def test_tokenize():
    assert tokenize("getRideStatus") == ["ride", "status"]
    assert tokenize("list_payment_methods") == ["list", "payment", "method"]


# ---- REST / OpenAPI bridging ---------------------------------------------------------
def test_build_input_schema_flattens_body_and_params():
    op = RestOperation(
        name="charge", method="POST", path="/u/{user_id}/charges",
        params=[RestParam(name="user_id", location="path"),
                RestParam(name="dry_run", location="query", schema={"type": "boolean"})],
        body_schema={"type": "object", "title": "Body", "properties": {"amount": {"type": "number", "title": "Amount"}},
                     "required": ["amount"]},
    )
    schema = build_input_schema(op)
    assert set(schema["properties"]) == {"user_id", "dry_run", "amount"}
    assert schema["required"] == ["user_id", "amount"]
    assert "title" not in schema["properties"]["amount"]
    assert schema["additionalProperties"] is False


def test_compact_schema_keeps_property_named_title():
    s = compact_schema({"type": "object", "title": "X", "properties": {"title": {"type": "string", "title": "T"}}})
    assert s == {"type": "object", "properties": {"title": {"type": "string"}}}


def test_openapi_conversion():
    doc = {
        "openapi": "3.1.0",
        "components": {"schemas": {"Charge": {"type": "object", "title": "Charge",
                                              "properties": {"amount": {"type": "number"},
                                                             "parent": {"$ref": "#/components/schemas/Charge"}},
                                              "required": ["amount"]}}},
        "paths": {
            "/charges/{id}": {
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "get": {"operationId": "get_charge", "summary": "Get a charge"},
                "put": {"operationId": "update_charge", "x-mcp-name": "update",
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Charge"}}}}},
                "delete": {"operationId": "internal", "x-mcp-exclude": True},
            },
        },
    }
    ops = {o.name: o for o in openapi_to_operations(doc)}
    assert set(ops) == {"get_charge", "update"}
    assert ops["get_charge"].params[0].location == "path"
    body = ops["update"].body_schema
    assert body["required"] == ["amount"] and "title" not in body
    assert "recursive" in body["properties"]["parent"]["description"]  # cycle is cut, not infinite


# ---- result bounding -----------------------------------------------------------------
def test_large_results_are_truncated():
    cfg = GatewayConfig()
    cfg.limits.max_result_bytes = 1000
    gw = Gateway.__new__(Gateway)
    gw.config = cfg
    big = types.CallToolResult(content=[types.TextContent(type="text", text="é" * 5000)])
    out = gw._bound_result(big)
    assert len(out.model_dump_json().encode()) < 1500
    assert "truncated by gateway" in out.content[0].text
    small = types.CallToolResult(content=[types.TextContent(type="text", text="ok")])
    assert gw._bound_result(small) is small


def test_metrics_render():
    m = Metrics()
    m.tool_calls.inc("rides", "rides__x", "ok")
    m.tool_latency.observe(0.02, "rides")
    text = m.render()
    assert 'mcpgw_tool_calls_total{server="rides",tool="rides__x",outcome="ok"} 1.0' in text
    assert 'mcpgw_tool_call_duration_seconds_bucket{server="rides",le="0.025"} 1' in text
