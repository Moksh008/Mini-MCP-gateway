import { useState } from "react";
import { http } from "../api";
import { Badge, Card, Collapsible, Empty, ErrorNote, JsonView, outcomeTone, timeAgo } from "../components/ui";
import { usePoll } from "../hooks";

const EVENTS = ["", "tools.call", "tools.search", "tools.describe", "tools.list", "auth.failure", "request.rate_limited",
  "admin.server.register", "admin.server.deregister", "admin.server.disable", "admin.server.enable", "admin.server.refresh"];
const OUTCOMES = ["", "ok", "tool_error", "tool_not_found", "invalid_arguments", "rate_limited", "upstream_unavailable",
  "upstream_timeout", "upstream_error", "unauthenticated"];

export function Audit({ adminKey }: { adminKey: string }) {
  const [principal, setPrincipal] = useState("");
  const [event, setEvent] = useState("");
  const [outcome, setOutcome] = useState("");
  const [paused, setPaused] = useState(false);

  const qs = new URLSearchParams({ limit: "200", ...(principal && { principal }), ...(event && { event }), ...(outcome && { outcome }) });
  const audit = usePoll(async () => (paused ? null : http(adminKey, `/admin/audit?${qs}`)), 2000, [adminKey, qs.toString(), paused]);
  const [frozen, setFrozen] = useState<any[]>([]);
  const events: any[] = paused ? frozen : audit.data?.events ?? [];

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Audit log</h1>
          <p className="muted">Every decision the gateway makes — discovery, calls, denials, auth failures, admin changes. Sensitive arguments are redacted;
            <code>args_sha256</code> lets you correlate with upstream logs without storing payloads.</p>
        </div>
        <button className={`btn ${paused ? "btn-primary" : "btn-ghost"}`}
                onClick={() => { if (!paused) setFrozen(events); setPaused(!paused); }}>
          {paused ? "▶ Resume live tail" : "❚❚ Pause"}
        </button>
      </div>
      <ErrorNote error={audit.error} />
      <Card>
        <div className="filters">
          <select value={principal} onChange={(e) => setPrincipal(e.target.value)}>
            <option value="">All principals</option>
            {["rider-bot", "care-copilot", "bi-analyst", "ops-admin"].map((p) => <option key={p}>{p}</option>)}
          </select>
          <select value={event} onChange={(e) => setEvent(e.target.value)}>
            {EVENTS.map((e) => <option key={e} value={e}>{e || "All events"}</option>)}
          </select>
          <select value={outcome} onChange={(e) => setOutcome(e.target.value)}>
            {OUTCOMES.map((o) => <option key={o} value={o}>{o || "All outcomes"}</option>)}
          </select>
          {!paused && <span className="live"><span className="dot dot-ok pulse" /> live</span>}
        </div>
        {events.length ? (
          <div className="audit-list">
            {events.map((e, i) => (
              <Collapsible key={`${e.ts}-${e.request_id}-${i}`} summary={
                <div className="audit-row">
                  <span className="audit-time" title={e.ts}>{timeAgo(e.ts)}</span>
                  <span className="audit-event">{e.event}</span>
                  <Badge tone={outcomeTone(e.outcome)}>{e.outcome ?? "—"}</Badge>
                  <span className="audit-principal">{e.principal ?? "anonymous"}</span>
                  <span className="audit-tool">{e.tool ? <code>{e.tool}</code> : e.query ? <span className="muted">“{e.query}”</span> : e.server ?? e.path ?? ""}</span>
                  {e.latency_ms != null && <span className="muted small">{e.latency_ms} ms</span>}
                  {e.mode && <span className="muted small">{e.mode}</span>}
                </div>
              }>
                {e.error_detail && <div className="callout callout-warn">Internal reason (never shown to the caller): {e.error_detail}</div>}
                <JsonView value={e} maxHeight={320} />
              </Collapsible>
            ))}
          </div>
        ) : <Empty>No matching events yet.</Empty>}
      </Card>
    </div>
  );
}
