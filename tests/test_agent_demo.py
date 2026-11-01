"""LLM agent (against a scripted mock provider), public-demo mode and SSRF guard."""

from __future__ import annotations

import json
import os

import httpx
import pytest

from mcp_gateway.app import create_app
from mcp_gateway.config import CircuitBreakerSpec, RateLimitSpec
from mcp_gateway.metrics import Metrics
from mcp_gateway.models import ToolDef
from mcp_gateway.netguard import UnsafeUpstream, check_upstream_url
from mcp_gateway.registry import Registry
from mcp_gateway.store import RegistryStore
from mcp_gateway.upstreams import StaticUpstream

from . import mock_llm
from .conftest import ServerThread, auth, build_config, free_port


@pytest.fixture(scope="module")
def llm():
    port = free_port()
    t = ServerThread(mock_llm.app, port)
    t.start()
    yield {"url": f"http://127.0.0.1:{port}", "app": mock_llm.app}
    t.stop()


@pytest.fixture(scope="module")
def demo_gw(backends, llm, tmp_path_factory):
    """A second gateway in public-demo mode, with the agent wired to the mock LLM."""
    os.environ["MOCK_LLM_KEY"] = "test-key-123"
    config = build_config(backends, tmp_path_factory.mktemp("demo"))
    config.demo.enabled = True
    config.demo.max_runtime_servers = 1
    config.limits.per_client_ip = True
    config.upstream_host_allowlist = ["127.0.0.1"]
    config.agent.base_url = llm["url"]
    config.agent.api_key_env = "MOCK_LLM_KEY"
    config.agent.run_rate = RateLimitSpec(per_minute=6, burst=5)
    port = free_port()
    t = ServerThread(create_app(config), port)
    t.start(timeout=60)
    yield f"http://127.0.0.1:{port}"
    t.stop()
    os.environ.pop("MOCK_LLM_KEY", None)


def run_agent(url: str, role: str, task: str, mode: str = "search", **extra) -> tuple[int, list[dict]]:
    with httpx.Client(base_url=url, timeout=60) as c:
        r = c.post("/v1/agent/run", headers=auth(role), json={"task": task, "mode": mode, **extra})
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.status_code, [r.json()]
        events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
        return r.status_code, events


# ---- agent ---------------------------------------------------------------------------
def test_agent_unavailable_without_key(http):
    meta = http.get("/v1/meta").json()  # public endpoint
    assert meta["public_demo"] is False
    assert meta["agent"]["available"] is False and "OPENROUTER_API_KEY" in meta["agent"]["reason"]
    r = http.post("/v1/agent/run", headers=auth("rider"), json={"task": "estimate a fare"})
    assert r.status_code == 503


def test_agent_incremental_run(demo_gw, llm):
    llm["app"].state.requests.clear()
    status, events = run_agent(demo_gw, "rider", "how much does a ride to the airport cost")
    assert status == 200
    kinds = [e["type"] for e in events]
    assert kinds[0] == "start" and kinds[-1] == "final"
    tools = [e for e in events if e["type"] == "tool"]
    assert [t["name"] for t in tools] == ["search_tools", "get_tool_schema", "call_tool"]
    assert tools[2]["target"] == "rides__estimate_fare" and not tools[2]["is_error"]
    assert "fare_usd" in tools[2]["result"]
    final = events[-1]
    assert final["totals"]["tools_used"] == ["rides__estimate_fare"]
    assert final["totals"]["llm_calls"] == 4 and final["totals"]["prompt_tokens"] > 0
    # the provider key stays server-side and only meta-tools were offered to the model
    sent = llm["app"].state.requests[0]
    assert sent["auth"] == "Bearer test-key-123"
    assert sent["body"]["model"] == "anthropic/claude-haiku-5.5"
    assert {t["function"]["name"] for t in sent["body"]["tools"]} == {"search_tools", "get_tool_schema", "call_tool"}


def test_agent_full_mode_offers_whole_catalog(demo_gw, llm):
    llm["app"].state.requests.clear()
    status, events = run_agent(demo_gw, "rider", "estimate a fare to the airport", mode="full")
    assert status == 200 and events[-1]["type"] == "final"
    assert events[0]["tools_offered"] == 18  # everything rider-bot may call
    full_first = llm["app"].state.requests[0]["body"]
    assert len(full_first["tools"]) == 18
    _, inc = run_agent(demo_gw, "rider", "estimate a fare to the airport", mode="search")
    first_llm = lambda evs: next(e for e in evs if e["type"] == "llm")["prompt_tokens"]  # noqa: E731
    assert first_llm(events) > 3 * first_llm(inc)  # full catalog costs far more context per call


def test_agent_is_bound_by_rbac(demo_gw):
    _, events = run_agent(demo_gw, "rider", "refund my last charge please")
    tool = next(e for e in events if e["type"] == "tool")
    assert tool["target"] == "payments__refund_payment"
    assert tool["is_error"] and "tool_not_found" in tool["result"]


def test_agent_rejects_unknown_model(demo_gw):
    status, body = run_agent(demo_gw, "rider", "estimate a fare", model="some/expensive-model")
    assert status == 400 and body[0]["error"] == "invalid_arguments"


def test_agent_run_rate_limit(demo_gw):
    codes = [run_agent(demo_gw, "analyst", "estimate a fare to the airport")[0] for _ in range(8)]
    assert 429 in codes


# ---- public demo mode ----------------------------------------------------------------
def test_demo_meta_and_hidden_ips(demo_gw):
    with httpx.Client(base_url=demo_gw, timeout=30) as c:
        meta = c.get("/v1/meta").json()
        assert meta["public_demo"] is True and meta["agent"]["available"] is True
        assert meta["demo"]["max_runtime_servers"] == 1
        c.get("/v1/whoami", headers=auth("rider"))
        events = c.get("/admin/audit", headers=auth("admin"), params={"limit": 50}).json()["events"]
        assert events and all("client_ip" not in e for e in events)


def test_demo_runtime_server_cap_and_ssrf(demo_gw, backends):
    spec = lambda name, url: {"name": name, "kind": "rest", "url": url,  # noqa: E731
                              "operations": [{"name": "geocode", "path": "/v1/geocode", "description": "x",
                                              "params": [{"name": "address", "location": "query", "required": True}]}]}
    maps = f"http://127.0.0.1:{backends['maps']}"
    with httpx.Client(base_url=demo_gw, timeout=30, headers=auth("admin")) as c:
        assert c.post("/admin/servers", json=spec("meta-svc", "http://169.254.169.254/latest")).status_code == 403
        assert c.post("/admin/servers", json=spec("evil-host", "http://example.com")).status_code == 403  # allowlist
        assert c.post("/admin/servers", json=spec("geo-a", maps)).status_code == 201
        assert c.post("/admin/servers", json=spec("geo-b", maps)).status_code == 429  # cap = 1
        assert c.post("/admin/servers", json=spec("geo-a", maps), params={"replace": "true"}).status_code == 201
        assert c.delete("/admin/servers/geo-a").status_code == 204


# ---- registry reset / expiry (unit) --------------------------------------------------
async def test_registry_reset_and_expiry(tmp_path):
    reg = Registry(store=RegistryStore(str(tmp_path / "r.db")), metrics=Metrics(), breaker_spec=CircuitBreakerSpec())
    tool = ToolDef(name="ping", description="ping", input_schema={"type": "object", "properties": {}})
    await reg.register_upstream(StaticUpstream("base", [(tool, lambda a: {"ok": True})]), source="config")
    for name in ("one", "two"):
        await reg.register_upstream(StaticUpstream(name, [(tool, lambda a: {"ok": True})]), source="api")
    await reg.set_enabled("base", False)
    reg.get_server("one").created_at -= 1000
    assert await reg.expire_runtime_servers(500, actor="t") == ["one"]
    assert sorted(await reg.reset_to_config(actor="t")) == ["two"]
    assert [s.spec.name for s in reg.servers()] == ["base"] and reg.get_server("base").spec.enabled
    await reg.close()


# ---- SSRF guard (unit) ---------------------------------------------------------------
@pytest.mark.parametrize("url,allow", [
    ("http://169.254.169.254/latest/meta-data", []),
    ("http://metadata.google.internal/computeMetadata/v1", []),
    ("http://[fe80::1]/", []),
    ("http://0.0.0.0:80/", []),
    ("file:///etc/passwd", []),
    ("http://internal.example:80/", ["rides.internal"]),
])
async def test_netguard_blocks(url, allow):
    with pytest.raises(UnsafeUpstream):
        await check_upstream_url(url, allow)


@pytest.mark.parametrize("url,allow", [
    ("http://127.0.0.1:9105", []),
    ("http://localhost:9105", ["localhost"]),
    ("https://rides.internal/mcp", ["rides.internal"]),
])
async def test_netguard_allows(url, allow):
    await check_upstream_url(url, allow)
