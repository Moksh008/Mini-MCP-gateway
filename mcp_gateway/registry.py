"""Central registry of upstream servers and the merged, namespaced tool catalog."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from .config import CircuitBreakerSpec
from .errors import ConfigError
from .metrics import Metrics
from .models import ServerSpec, ToolDef, ToolRecord, qualify
from .store import RegistryStore
from .upstreams import Upstream, create_upstream

log = logging.getLogger(__name__)

Source = Literal["config", "api", "embedded"]


class CircuitBreaker:
    """closed → (N consecutive failures) → open → (cooldown) → half-open → closed/open."""

    def __init__(self, spec: CircuitBreakerSpec, clock=time.monotonic) -> None:
        self._spec, self._clock = spec, clock
        self.failures = 0
        self.opened_at: float | None = None
        self._trial_in_flight = False

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if self._clock() - self.opened_at >= self._spec.reset_after_s else "open"

    def allow(self) -> bool:
        state = self.state
        if state == "closed":
            return True
        if state == "half_open" and not self._trial_in_flight:
            self._trial_in_flight = True
            return True
        return False

    def retry_after(self) -> float:
        if self.opened_at is None:
            return 0.0
        return max(0.0, self._spec.reset_after_s - (self._clock() - self.opened_at))

    def record_success(self) -> None:
        self.failures, self.opened_at, self._trial_in_flight = 0, None, False

    def record_failure(self) -> None:
        self.failures += 1
        if self._trial_in_flight or self.failures >= self._spec.failure_threshold:
            self.opened_at = self._clock()
        self._trial_in_flight = False


@dataclass
class ServerState:
    spec: ServerSpec
    upstream: Upstream
    source: Source
    breaker: CircuitBreaker
    semaphore: asyncio.Semaphore
    tools: list[ToolRecord] = field(default_factory=list)
    last_refresh: float | None = None
    refresh_error: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "name": self.spec.name,
            "kind": self.spec.kind,
            "description": self.spec.description,
            "owner": self.spec.owner,
            "tags": self.spec.tags,
            "enabled": self.spec.enabled,
            "source": self.source,
            "healthy": self.upstream.healthy and self.refresh_error is None,
            "last_error": self.refresh_error or self.upstream.last_error,
            "circuit": self.breaker.state,
            "tool_count": len(self.tools),
            "last_refresh_unix": self.last_refresh,
            "created_unix": self.created_at,
            "url": self.spec.url,
        }


class Registry:
    def __init__(
        self, *, store: RegistryStore, metrics: Metrics, breaker_spec: CircuitBreakerSpec,
        upstream_factory=create_upstream,
    ) -> None:
        self._store = store
        self._metrics = metrics
        self._breaker_spec = breaker_spec
        self._factory = upstream_factory
        self._servers: dict[str, ServerState] = {}
        self._catalog: dict[str, ToolRecord] = {}
        self._lock = asyncio.Lock()
        self._refresh_tasks: set[asyncio.Task] = set()
        self._bg: asyncio.Task | None = None
        self.version = 0

    # ---- lifecycle -------------------------------------------------------------------
    async def bootstrap(self, config_servers: list[ServerSpec]) -> None:
        names = {s.name for s in config_servers}
        stored = [s for s in self._store.load_all() if s.name not in names]
        await asyncio.gather(
            *(self._add(s, "config") for s in config_servers),
            *(self._add(s, "api") for s in stored),
        )

    def start_background_refresh(self, interval_s: float) -> None:
        if interval_s > 0:
            self._bg = asyncio.create_task(self._refresh_loop(interval_s), name="registry-refresh")

    async def _refresh_loop(self, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            with contextlib.suppress(Exception):
                await self.refresh_all()

    async def close(self) -> None:
        if self._bg:
            self._bg.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._bg
        for t in list(self._refresh_tasks):
            t.cancel()
        await asyncio.gather(*(s.upstream.stop() for s in self._servers.values()), return_exceptions=True)
        self._servers.clear()
        self._rebuild()

    # ---- mutation --------------------------------------------------------------------
    async def register(
        self, spec: ServerSpec, *, actor: str, persist: bool = True, replace: bool = False,
    ) -> ServerState:
        async with self._lock:
            existing = self._servers.get(spec.name)
            if existing and existing.source == "config":
                raise ConfigError(f"server '{spec.name}' is defined in the config file and cannot be replaced")
            if existing and not replace:
                raise ConfigError(f"server '{spec.name}' is already registered (use replace=true)")
        if existing:
            await self.deregister(spec.name, actor=actor, persist=False)
        state = await self._add(spec, "api")
        if persist:
            self._store.upsert(spec, actor)
        return state

    async def register_upstream(self, upstream: Upstream, source: Source = "embedded") -> ServerState:
        """Register a pre-built upstream (e.g. :class:`StaticUpstream`)."""
        return await self._add(upstream.spec, source, upstream)

    async def deregister(self, name: str, *, actor: str, persist: bool = True) -> None:
        async with self._lock:
            state = self._servers.get(name)
            if state is None:
                raise KeyError(name)
            if state.source == "config" and persist:
                raise ConfigError(f"server '{name}' is defined in the config file; remove it there")
            del self._servers[name]
            self._rebuild()
        await state.upstream.stop()
        if persist:
            self._store.delete(name)

    async def set_enabled(self, name: str, enabled: bool) -> ServerState:
        async with self._lock:
            state = self._servers[name]
            state.spec = state.spec.model_copy(update={"enabled": enabled})
            self._rebuild()
            return state

    async def _add(self, spec: ServerSpec, source: Source, upstream: Upstream | None = None) -> ServerState:
        if spec.name in self._servers:
            raise ConfigError(f"server '{spec.name}' is already registered")
        upstream = upstream or self._factory(spec)
        state = ServerState(
            spec=spec, upstream=upstream, source=source,
            breaker=CircuitBreaker(self._breaker_spec),
            semaphore=asyncio.Semaphore(spec.max_concurrency),
        )
        upstream.set_tools_changed_callback(self._schedule_refresh)
        await upstream.start()
        async with self._lock:
            if spec.name in self._servers:
                await upstream.stop()
                raise ConfigError(f"server '{spec.name}' is already registered")
            self._servers[spec.name] = state
        await self.refresh(spec.name)
        return state

    def _schedule_refresh(self, name: str) -> None:
        task = asyncio.get_running_loop().create_task(self.refresh(name))
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)

    async def refresh(self, name: str) -> ServerState:
        state = self._servers[name]
        try:
            defs = await asyncio.wait_for(state.upstream.list_tools(), timeout=state.spec.timeout_s)
            records = [self._to_record(state.spec, d) for d in defs]
            state.refresh_error = None
        except Exception as e:  # noqa: BLE001 - keep last known-good catalog on failure
            state.refresh_error = getattr(e, "detail", None) or f"{type(e).__name__}: {e}"
            log.warning("refresh of %s failed: %s", name, state.refresh_error)
            records = state.tools
        async with self._lock:
            if self._servers.get(name) is state:
                seen: set[str] = set()
                deduped = []
                for r in records:
                    if r.qualified_name in seen:
                        log.warning("dropping duplicate tool name %s", r.qualified_name)
                        continue
                    seen.add(r.qualified_name)
                    deduped.append(r)
                state.tools = deduped
                state.last_refresh = time.time()
                self._rebuild()
        return state

    def runtime_servers(self) -> list[ServerState]:
        return [s for s in self._servers.values() if s.source == "api"]

    async def reset_to_config(self, *, actor: str) -> list[str]:
        """Drop every runtime-registered server and re-enable config servers (demo reset)."""
        removed = []
        for state in self.runtime_servers():
            with contextlib.suppress(KeyError):
                await self.deregister(state.spec.name, actor=actor)
                removed.append(state.spec.name)
        async with self._lock:
            for state in self._servers.values():
                state.breaker.record_success()
                if not state.spec.enabled:
                    state.spec = state.spec.model_copy(update={"enabled": True})
            self._rebuild()
        return removed

    async def expire_runtime_servers(self, ttl_s: float, *, actor: str) -> list[str]:
        cutoff = time.time() - ttl_s
        expired = []
        for state in self.runtime_servers():
            if state.created_at < cutoff:
                with contextlib.suppress(KeyError):
                    await self.deregister(state.spec.name, actor=actor)
                    expired.append(state.spec.name)
        return expired

    async def refresh_all(self) -> None:
        await asyncio.gather(*(self.refresh(n) for n in list(self._servers)), return_exceptions=True)

    @staticmethod
    def _to_record(spec: ServerSpec, d: ToolDef) -> ToolRecord:
        return ToolRecord(
            qualified_name=qualify(spec.name, d.name), server=spec.name, upstream_name=d.name,
            description=d.description, input_schema=d.input_schema, output_schema=d.output_schema,
            annotations=d.annotations, title=d.title, tags=tuple(spec.tags),
        )

    def _rebuild(self) -> None:
        self._catalog = {
            t.qualified_name: t for s in self._servers.values() if s.spec.enabled for t in s.tools
        }
        self.version += 1
        self._metrics.registry_tools.set(value=len(self._catalog))
        for name, s in self._servers.items():
            self._metrics.upstream_up.set(name, value=1.0 if s.upstream.healthy else 0.0)

    # ---- queries ---------------------------------------------------------------------
    def tools(self) -> list[ToolRecord]:
        return list(self._catalog.values())

    def get_tool(self, qualified_name: str) -> ToolRecord | None:
        return self._catalog.get(qualified_name)

    def get_server(self, name: str) -> ServerState | None:
        return self._servers.get(name)

    def servers(self) -> list[ServerState]:
        return list(self._servers.values())
