import { useEffect, useRef, useState } from "react";
import { fmt, http } from "../api";
import { Sparkbars } from "../components/charts";
import { Badge, Card, Dot, ErrorNote, KIND_LABEL, Stat } from "../components/ui";
import { usePoll } from "../hooks";

const GROUPS = ["ok", "tool_error", "denied", "rate_limited", "failed"] as const;
const COLORS = ["#2ee59d", "#f5b84b", "#8b93a7", "#e0a3ff", "#ff6b6b"];
const LABELS = ["ok", "tool error", "denied / invalid", "rate limited", "upstream failure"];

function group(outcome: string): (typeof GROUPS)[number] {
  if (outcome === "ok") return "ok";
  if (outcome === "tool_error") return "tool_error";
  if (outcome === "rate_limited") return "rate_limited";
  if (outcome === "tool_not_found" || outcome === "invalid_arguments" || outcome === "permission_denied") return "denied";
  return "failed";
}

/** Samples /admin/stats continuously (mounted at app level so the traffic chart survives navigation). */
export function useTraffic(adminKey: string | null) {
  const stats = usePoll(async () => (adminKey ? http(adminKey, "/admin/stats") : null), 2500, [adminKey]);
  const last = useRef<Record<string, number> | null>(null);
  const [history, setHistory] = useState<Record<string, number[]>>(() => Object.fromEntries(GROUPS.map((g) => [g, []])));
  useEffect(() => {
    if (!stats.data) return;
    const totals: Record<string, number> = Object.fromEntries(GROUPS.map((g) => [g, 0]));
    for (const c of stats.data.tool_calls) totals[group(c.outcome)] += c.count;
    const prev = last.current;
    last.current = totals;
    if (!prev) return;
    setHistory((h) => Object.fromEntries(GROUPS.map((g) => [g, [...h[g], Math.max(0, totals[g] - prev[g])].slice(-60)])));
  }, [stats.data]);
  return { stats, history };
}

export type Traffic = ReturnType<typeof useTraffic>;

export function Overview({ adminKey, traffic, onNavigate }: { adminKey: string; traffic: Traffic; onNavigate: (p: string) => void }) {
  const { stats, history } = traffic;
  const servers = usePoll(() => http(adminKey, "/admin/servers"), 4000, [adminKey]);

  const s = stats.data;
  const calls = s?.tool_calls ?? [];
  const total = calls.reduce((a: number, c: any) => a + c.count, 0);
  const ok = calls.filter((c: any) => c.outcome === "ok").reduce((a: number, c: any) => a + c.count, 0);
  const rateLimited = Object.values(s?.rate_limited ?? {}).reduce((a: number, b: any) => a + b, 0) as number;
  const discovery = s?.discovery ?? {};

  const byTool = Object.entries(
    calls.reduce((acc: Record<string, number>, c: any) => ({ ...acc, [c.tool]: (acc[c.tool] ?? 0) + c.count }), {}),
  ).sort((a: any, b: any) => b[1] - a[1]).slice(0, 8) as [string, number][];

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Overview</h1>
          <p className="muted">Live state of the gateway: upstreams, catalog and traffic. Refreshes every few seconds.</p>
        </div>
        <button className="btn btn-primary" onClick={() => onNavigate("playground")}>Open Agent Playground →</button>
      </div>
      <ErrorNote error={stats.error ?? servers.error} />

      <div className="stats">
        <Stat label="Upstream servers" value={s ? `${s.healthy_servers}/${s.servers}` : "—"} sub="healthy / registered"
              tone={s && s.healthy_servers < s.servers ? "bad" : "ok"} />
        <Stat label="Tools in catalog" value={fmt(s?.tools)} sub={`registry v${s?.registry_version ?? "—"}`} />
        <Stat label="Tool calls" value={fmt(total)} sub={total ? `${((100 * ok) / total).toFixed(0)}% ok` : "since start"} />
        <Stat label="Discovery requests" value={fmt((discovery.search ?? 0) + (discovery.describe ?? 0) + (discovery.list ?? 0) + (discovery.list_meta ?? 0))}
              sub={`${discovery.search ?? 0} searches · ${discovery.describe ?? 0} schema fetches`} />
        <Stat label="Rate limited" value={fmt(rateLimited)} tone={rateLimited ? "warn" : undefined} sub="requests + tool calls" />
        <Stat label="Auth failures" value={fmt(s?.auth_failures)} tone={s?.auth_failures ? "warn" : undefined} sub="rejected credentials" />
      </div>

      <div className="grid-2">
        <Card title="Tool-call traffic" actions={<span className="muted small">per 2.5 s · last 60 samples</span>}>
          <Sparkbars series={GROUPS.map((g) => ({ label: g, values: history[g] }))} colors={COLORS} height={110} />
          <div className="legend">{LABELS.map((l, i) => <span key={l}><i className="sw" style={{ background: COLORS[i] }} />{l}</span>)}</div>
        </Card>
        <Card title="Most-called tools">
          {byTool.length ? (
            <ul className="toplist">
              {byTool.map(([name, n]) => (
                <li key={name}><code>{name}</code><span className="toplist-bar"><span style={{ width: `${(100 * n) / byTool[0][1]}%` }} /></span><b>{n}</b></li>
              ))}
            </ul>
          ) : <div className="muted small">No calls yet — try the Playground or Scenarios.</div>}
        </Card>
      </div>

      <Card title="Upstreams" actions={<button className="btn btn-ghost btn-sm" onClick={() => onNavigate("registry")}>Manage registry →</button>}>
        <div className="upstreams">
          {(servers.data?.servers ?? []).map((sv: any) => {
            const lat = s?.latency?.[sv.name];
            return (
              <div key={sv.name} className={`upstream ${sv.enabled ? "" : "disabled"}`}>
                <div className="upstream-top">
                  <Dot ok={sv.enabled ? sv.healthy : null} pulse={sv.healthy && sv.enabled} />
                  <strong>{sv.name}</strong>
                  <Badge tone="neutral">{KIND_LABEL[sv.kind] ?? sv.kind}</Badge>
                </div>
                <div className="upstream-desc">{sv.description}</div>
                <div className="upstream-meta">
                  <span><b>{sv.tool_count}</b> tools</span>
                  <span>circuit <Badge tone={sv.circuit === "closed" ? "ok" : "bad"}>{sv.circuit}</Badge></span>
                  {lat && <span>p50 <b>{lat.p50_ms ?? "—"}</b> ms · p95 <b>{lat.p95_ms ?? "—"}</b> ms</span>}
                  {!sv.enabled && <Badge tone="warn">disabled</Badge>}
                </div>
                {sv.last_error && sv.enabled && !sv.healthy && <div className="upstream-err">{sv.last_error}</div>}
                <div className="upstream-owner muted small">owner: {sv.owner || "—"} · source: {sv.source}</div>
              </div>
            );
          })}
        </div>
      </Card>
    </div>
  );
}
