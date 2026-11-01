"""Shared fixtures: real backends + a real gateway on free ports, driven over HTTP."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from mcp_gateway.app import create_app
from mcp_gateway.config import GatewayConfig

ROOT = Path(__file__).resolve().parents[1]
KEYS = {"admin": "demo-admin-key", "rider": "demo-rider-key", "support": "demo-support-key",
        "analyst": "demo-analyst-key"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerThread:
    def __init__(self, app, port: int) -> None:
        self.port = port
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self, timeout: float = 30) -> None:
        self.thread.start()
        deadline = time.time() + timeout
        while not self.server.started:
            if time.time() > deadline or not self.thread.is_alive():
                raise RuntimeError(f"server on port {self.port} failed to start")
            time.sleep(0.05)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture(scope="session")
def backends():
    from examples.backends import maps_api, payments_api, rides_mcp, support_mcp

    ports = {}
    threads = []
    for name, app in (("rides", rides_mcp.app), ("support", support_mcp.app),
                      ("payments", payments_api.app), ("maps", maps_api.app)):
        port = free_port()
        t = ServerThread(app, port)
        t.start()
        ports[name] = port
        threads.append(t)
    yield ports
    for t in threads:
        t.stop()


def build_config(ports: dict[str, int], tmp: Path) -> GatewayConfig:
    data = yaml.safe_load((ROOT / "examples" / "gateway.yaml").read_text(encoding="utf-8"))
    for s in data["servers"]:
        if s["name"] in ports:
            path = "/mcp" if s["kind"] == "mcp_http" else ""
            s["url"] = f"http://127.0.0.1:{ports[s['name']]}{path}"
        if s.get("cwd"):
            s["cwd"] = str(ROOT)
    data["registry_db"] = str(tmp / "registry.db")
    data["audit"] = {"path": str(tmp / "audit.jsonl")}
    data["circuit_breaker"] = {"failure_threshold": 2, "reset_after_s": 60}
    data["refresh_interval_s"] = 0
    data["metrics_public"] = True
    return GatewayConfig.model_validate(data)


@pytest.fixture(scope="session")
def gateway(backends, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gw")
    config = build_config(backends, tmp)
    app = create_app(config)
    port = free_port()
    t = ServerThread(app, port)
    t.start(timeout=60)
    yield {"url": f"http://127.0.0.1:{port}", "app": app, "config": config, "tmp": tmp, "backends": backends}
    t.stop()


@pytest.fixture
def http(gateway):
    with httpx.Client(base_url=gateway["url"], timeout=30) as c:
        yield c


def auth(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {KEYS[role]}"}


class MCP:
    """Open an MCP client session against the gateway: ``async with MCP(url, key, full) as s``."""

    def __init__(self, url: str, role: str, full: bool = False) -> None:
        self.url = url + ("/mcp/full" if full else "/mcp")
        self.headers = auth(role)

    async def __aenter__(self) -> ClientSession:
        self._t = streamablehttp_client(self.url, headers=self.headers)
        r, w, _ = await self._t.__aenter__()
        self._s = ClientSession(r, w)
        session = await self._s.__aenter__()
        await session.initialize()
        return session

    async def __aexit__(self, *exc) -> None:
        await self._s.__aexit__(*exc)
        await self._t.__aexit__(*exc)
