"""Minimal dependency-free Prometheus metrics (text exposition format 0.0.4)."""

from __future__ import annotations

import bisect
import threading
from collections import defaultdict


def _fmt_labels(names: tuple[str, ...], values: tuple[str, ...], extra: str = "") -> str:
    parts = [f'{n}="{_escape(v)}"' for n, v in zip(names, values, strict=True)]
    if extra:
        parts.append(extra)
    return "{" + ",".join(parts) + "}" if parts else ""


def _escape(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Counter:
    def __init__(self, name: str, help_: str, labels: tuple[str, ...] = ()) -> None:
        self.name, self.help, self.labels = name, help_, labels
        self._values: dict[tuple[str, ...], float] = defaultdict(float)
        self._lock = threading.Lock()

    def inc(self, *label_values: str, amount: float = 1.0) -> None:
        with self._lock:
            self._values[tuple(label_values)] += amount

    def value(self, *label_values: str) -> float:
        return self._values.get(tuple(label_values), 0.0)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        for lv, v in sorted(self._values.items()):
            lines.append(f"{self.name}{_fmt_labels(self.labels, lv)} {v}")
        return lines


class Gauge(Counter):
    def set(self, *label_values: str, value: float) -> None:
        with self._lock:
            self._values[tuple(label_values)] = value

    def render(self) -> list[str]:
        lines = super().render()
        lines[1] = f"# TYPE {self.name} gauge"
        return lines


class Histogram:
    DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

    def __init__(self, name: str, help_: str, labels: tuple[str, ...] = (), buckets=DEFAULT_BUCKETS) -> None:
        self.name, self.help, self.labels = name, help_, labels
        self.buckets = tuple(sorted(buckets))
        self._counts: dict[tuple[str, ...], list[int]] = {}
        self._sums: dict[tuple[str, ...], float] = defaultdict(float)
        self._lock = threading.Lock()

    def observe(self, value: float, *label_values: str) -> None:
        key = tuple(label_values)
        with self._lock:
            counts = self._counts.setdefault(key, [0] * (len(self.buckets) + 1))
            counts[bisect.bisect_left(self.buckets, value)] += 1
            self._sums[key] += value

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for key, counts in sorted(self._counts.items()):
            cumulative = 0
            for bound, c in zip(self.buckets, counts, strict=False):
                cumulative += c
                le = 'le="' + str(bound) + '"'
                lines.append(f"{self.name}_bucket{_fmt_labels(self.labels, key, le)} {cumulative}")
            cumulative += counts[-1]
            le_inf = 'le="+Inf"'
            lines.append(f"{self.name}_bucket{_fmt_labels(self.labels, key, le_inf)} {cumulative}")
            lines.append(f"{self.name}_sum{_fmt_labels(self.labels, key)} {self._sums[key]}")
            lines.append(f"{self.name}_count{_fmt_labels(self.labels, key)} {cumulative}")
        return lines


class Metrics:
    def __init__(self) -> None:
        self.tool_calls = Counter(
            "mcpgw_tool_calls_total", "Tool calls by server, tool and outcome", ("server", "tool", "outcome")
        )
        self.tool_latency = Histogram(
            "mcpgw_tool_call_duration_seconds", "Upstream tool call latency", ("server",)
        )
        self.discovery = Counter(
            "mcpgw_discovery_requests_total", "tools/list, search and describe requests", ("operation",)
        )
        self.auth_failures = Counter("mcpgw_auth_failures_total", "Rejected authentication attempts")
        self.rate_limited = Counter("mcpgw_rate_limited_total", "Requests rejected by rate limits", ("scope",))
        self.upstream_up = Gauge("mcpgw_upstream_up", "1 if the upstream is connected and healthy", ("server",))
        self.registry_tools = Gauge("mcpgw_registry_tools", "Number of tools in the catalog", ())
        self.circuit_open = Gauge("mcpgw_circuit_open", "1 if the server circuit breaker is open", ("server",))

    def snapshot(self) -> dict:
        """JSON view of the metrics for dashboards (the Prometheus endpoint stays canonical)."""
        calls = [
            {"server": k[0], "tool": k[1], "outcome": k[2], "count": int(v)}
            for k, v in sorted(self.tool_calls._values.items())
        ]
        latency = {}
        for key, counts in self.tool_latency._counts.items():
            total = sum(counts)
            latency[key[0]] = {
                "count": total,
                "avg_ms": round(1000 * self.tool_latency._sums[key] / total, 2) if total else 0.0,
                "p50_ms": _quantile_ms(self.tool_latency.buckets, counts, 0.50),
                "p95_ms": _quantile_ms(self.tool_latency.buckets, counts, 0.95),
            }
        return {
            "tool_calls": calls,
            "latency": latency,
            "discovery": {k[0]: int(v) for k, v in self.discovery._values.items()},
            "auth_failures": int(self.auth_failures.value()),
            "rate_limited": {k[0]: int(v) for k, v in self.rate_limited._values.items()},
            "registry_tools": int(self.registry_tools.value()),
        }

    def render(self) -> str:
        lines: list[str] = []
        for m in (self.tool_calls, self.tool_latency, self.discovery, self.auth_failures,
                  self.rate_limited, self.upstream_up, self.registry_tools, self.circuit_open):
            lines.extend(m.render())
        return "\n".join(lines) + "\n"


def _quantile_ms(buckets: tuple[float, ...], counts: list[int], q: float) -> float | None:
    """Upper bucket bound containing quantile ``q`` (the usual histogram approximation)."""
    total = sum(counts)
    if not total:
        return None
    target, running = q * total, 0
    for bound, c in zip(buckets, counts, strict=False):
        running += c
        if running >= target:
            return round(bound * 1000, 1)
    return round(buckets[-1] * 1000, 1)
