import { Fragment, useState } from "react";
import { http } from "../api";
import { Badge, Card, Dot, ErrorNote, JsonView, KIND_LABEL, timeAgo } from "../components/ui";
import { usePoll } from "../hooks";

// Templates resolve against the *gateway's* network: in Docker the backends are at http://backends:*.
const TEMPLATES: Record<string, { label: string; help: string; spec: (host: string) => object }> = {
  rest: {
    label: "Legacy REST service",
    help: "Bridges two endpoints of the maps backend under a new name — no code, just a declaration.",
    spec: (host) => ({
      name: "geo-v2", kind: "rest", url: `http://${host}:9105`, owner: "maps", description: "Geocoding v2 (declared REST)",
      tags: ["maps"],
      operations: [
        { name: "geocode", path: "/v1/geocode", description: "Convert an address into coordinates.",
          params: [{ name: "address", location: "query", required: true }] },
        { name: "reverse_geocode", path: "/v1/reverse-geocode", description: "Nearest address for coordinates.",
          params: [{ name: "lat", location: "query", required: true, schema: { type: "number" } },
                   { name: "lng", location: "query", required: true, schema: { type: "number" } }] },
      ],
    }),
  },
  openapi: {
    label: "OpenAPI service",
    help: "Points at a service's OpenAPI document; every operation becomes a tool automatically.",
    spec: (host) => ({
      name: "billing", kind: "openapi", url: `http://${host}:9104`, owner: "payments", description: "Payments API via OpenAPI",
      headers: { Authorization: "Bearer ${PAYMENTS_API_TOKEN:-dev-payments-token}" },
      include_operations: ["get_invoice", "list_payment_methods"],
    }),
  },
  mcp: {
    label: "MCP server (HTTP)",
    help: "Registers the support MCP server a second time under another namespace.",
    spec: (host) => ({ name: "care", kind: "mcp_http", url: `http://${host}:9103/mcp`, owner: "customer-care",
                       description: "Support MCP (second registration)" }),
  },
  dead: {
    label: "Broken upstream",
    help: "Nothing listens on this port — use it to watch health checks and the circuit breaker react.",
    spec: () => ({ name: "flaky-svc", kind: "rest", url: "http://127.0.0.1:1", timeout_s: 2, description: "Always down",
                   operations: [{ name: "ping", path: "/ping", description: "Ping a service that is down" }] }),
  },
};

export function Registry({ adminKey, backendHost }: { adminKey: string; backendHost: string }) {
  const servers = usePoll(() => http(adminKey, "/admin/servers"), 4000, [adminKey]);
  const [template, setTemplate] = useState("rest");
  const [specText, setSpecText] = useState(() => JSON.stringify(TEMPLATES.rest.spec(backendHost), null, 2));
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ tone: string; text: string } | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [detail, setDetail] = useState<any>(null);

  const act = async (label: string, fn: () => Promise<any>, success: string) => {
    setBusy(label); setMsg(null);
    try {
      await fn();
      setMsg({ tone: "ok", text: success });
      await servers.refresh();
      if (expanded) setDetail(await http(adminKey, `/admin/servers/${expanded}`));
    } catch (e: any) {
      setMsg({ tone: "bad", text: e.message });
    } finally { setBusy(null); }
  };

  const register = (replace: boolean) => {
    let spec: any;
    try { spec = JSON.parse(specText); } catch (e: any) { setMsg({ tone: "bad", text: `Invalid JSON: ${e.message}` }); return; }
    act("register", () => http(adminKey, `/admin/servers${replace ? "?replace=true" : ""}`, { method: "POST", body: JSON.stringify(spec) }),
        `Registered '${spec.name}' — its tools are now discoverable (respecting RBAC).`);
  };

  const toggle = async (name: string) => {
    if (expanded === name) { setExpanded(null); return; }
    setExpanded(name);
    setDetail(await http(adminKey, `/admin/servers/${name}`).catch(() => null));
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Server registry</h1>
          <p className="muted">One place to see, add and operate every upstream. Config-file servers are read-only here; runtime ones persist in SQLite.</p>
        </div>
      </div>
      <ErrorNote error={servers.error} />
      {msg && <div className={`callout callout-${msg.tone}`}>{msg.text}</div>}

      <Card title={`Registered servers (${servers.data?.servers.length ?? 0})`}>
        <table className="table">
          <thead><tr><th /><th>Name</th><th>Kind</th><th>Owner</th><th>Tools</th><th>Circuit</th><th>Source</th><th>Refreshed</th><th /></tr></thead>
          <tbody>
            {(servers.data?.servers ?? []).map((s: any) => (
              <Fragment key={s.name}>
                <tr className={`clickable ${s.enabled ? "" : "row-disabled"}`} onClick={() => toggle(s.name)}>
                  <td><Dot ok={s.enabled ? s.healthy : null} /></td>
                  <td><strong>{s.name}</strong><div className="muted small">{s.description}</div></td>
                  <td><Badge>{KIND_LABEL[s.kind] ?? s.kind}</Badge></td>
                  <td>{s.owner || "—"}</td>
                  <td>{s.tool_count}</td>
                  <td><Badge tone={s.circuit === "closed" ? "ok" : "bad"}>{s.circuit}</Badge></td>
                  <td><Badge tone={s.source === "config" ? "neutral" : "info"}>{s.source}</Badge></td>
                  <td className="muted small">{s.last_refresh_unix ? timeAgo(new Date(s.last_refresh_unix * 1000).toISOString()) : "—"}</td>
                  <td className="actions" onClick={(e) => e.stopPropagation()}>
                    <button className="btn btn-ghost btn-sm" disabled={!!busy}
                            onClick={() => act(`r-${s.name}`, () => http(adminKey, `/admin/servers/${s.name}/refresh`, { method: "POST" }), `Refreshed ${s.name}.`)}>Refresh</button>
                    <button className="btn btn-ghost btn-sm" disabled={!!busy}
                            onClick={() => act(`t-${s.name}`, () => http(adminKey, `/admin/servers/${s.name}/${s.enabled ? "disable" : "enable"}`, { method: "POST" }),
                                               `${s.enabled ? "Disabled" : "Enabled"} ${s.name}.`)}>{s.enabled ? "Disable" : "Enable"}</button>
                    {s.source !== "config" && (
                      <button className="btn btn-danger btn-sm" disabled={!!busy}
                              onClick={() => act(`d-${s.name}`, () => http(adminKey, `/admin/servers/${s.name}`, { method: "DELETE" }), `Removed ${s.name}.`)}>Remove</button>
                    )}
                  </td>
                </tr>
                {expanded === s.name && (
                  <tr className="detail-row">
                    <td colSpan={9}>
                      {s.last_error && <div className="callout callout-bad">Last error: {s.last_error}</div>}
                      <div className="muted small">Endpoint: <code>{s.url ?? "(stdio process)"}</code></div>
                      <div className="tool-grid compact">
                        {(detail?.tools ?? []).map((t: string) => <code key={t} className="tool-chip">{t}</code>)}
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </Card>

      <Card title="Register a server at runtime" actions={<span className="muted small">POST /admin/servers</span>}>
        <div className="segmented">
          {Object.entries(TEMPLATES).map(([k, t]) => (
            <button key={k} className={template === k ? "active" : ""}
                    onClick={() => { setTemplate(k); setSpecText(JSON.stringify(t.spec(backendHost), null, 2)); }}>{t.label}</button>
          ))}
        </div>
        <p className="muted small">{TEMPLATES[template].help} Upstream secrets use <code>{"${ENV_VAR}"}</code> and are expanded inside the gateway — never stored or returned.</p>
        <textarea className="code-input" rows={14} value={specText} onChange={(e) => setSpecText(e.target.value)} spellCheck={false} />
        <div className="row-end">
          <button className="btn btn-ghost" disabled={!!busy} onClick={() => register(true)}>Replace existing</button>
          <button className="btn btn-primary" disabled={!!busy} onClick={() => register(false)}>{busy === "register" ? "Registering…" : "Register server"}</button>
        </div>
        <details className="muted small"><summary>Guards enforced by the gateway</summary>
          <JsonView value={{ "mcp_stdio via API": "rejected unless allow_stdio_registration: true (it would run a command on the gateway host)",
                             "URL schemes": "http(s) only", "upstream_host_allowlist": "optional SSRF guard",
                             "config-owned servers": "cannot be replaced or removed via API" }} />
        </details>
      </Card>
    </div>
  );
}
