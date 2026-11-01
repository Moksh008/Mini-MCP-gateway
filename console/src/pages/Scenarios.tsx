import { useState } from "react";
import { ApiError, DEMO_IDENTITIES, http, mcp, toolPayload, toolText } from "../api";
import { Badge, Card } from "../components/ui";

// Guided demos: each runs real requests against the gateway and asserts the expected behaviour,
// like a live acceptance test you can watch.

type Step = { label: string; ok: boolean; detail: string };
type Ctx = { log: (s: Step) => void };

const key = (id: string) => DEMO_IDENTITIES.find((i) => i.id === id)!.key;
const SF = { pickup_lat: 37.7749, pickup_lng: -122.4194, dropoff_lat: 37.6213, dropoff_lng: -122.379 };

async function call(id: string, name: string, args: object) {
  const r = await mcp(key(id), "/mcp", "tools/call", { name: "call_tool", arguments: { name, arguments: args } }, id);
  return { isError: !!r.result?.isError, text: toolText(r.result), payload: toolPayload(r.result), meta: r.result?._meta, raw: r };
}

const SCENARIOS: { id: string; title: string; story: string; run: (c: Ctx) => Promise<void> }[] = [
  {
    id: "rbac",
    title: "Role-based access control",
    story: "The same refund is denied for the rider assistant and allowed for the care copilot. Read-only analysts can price a ride but not book one.",
    run: async ({ log }) => {
      const ch = await call("care-copilot", "payments__charge_payment", { user_id: "u1", amount_usd: 20, method_id: "pm_visa_4242", description: "Scenario charge" });
      log({ label: "care-copilot charges $20 (OpenAPI upstream, credential injected by gateway)", ok: !ch.isError, detail: ch.isError ? ch.text : `charge ${ch.payload.charge_id}` });
      const denied = await call("rider-bot", "payments__refund_payment", { charge_id: ch.payload?.charge_id ?? "x" });
      log({ label: "rider-bot tries to refund it → denied", ok: denied.isError && denied.text.includes("tool_not_found"), detail: denied.text });
      const ok = await call("care-copilot", "payments__refund_payment", { charge_id: ch.payload?.charge_id ?? "x", amount_usd: 5, reason: "demo" });
      log({ label: "care-copilot refunds $5 → allowed", ok: !ok.isError && ok.payload?.status === "partially_refunded", detail: ok.isError ? ok.text : `status ${ok.payload.status}` });
      const ro = await call("bi-analyst", "rides__estimate_fare", SF);
      log({ label: "bi-analyst (read-only) estimates a fare → allowed", ok: !ro.isError, detail: ro.isError ? ro.text : `$${ro.payload.fare_usd}` });
      const w = await call("bi-analyst", "rides__request_ride", { rider_id: "u1", ...SF });
      log({ label: "bi-analyst tries to book a ride → denied (write tool)", ok: w.isError, detail: w.text });
    },
  },
  {
    id: "discovery",
    title: "Discovery only shows what you may use",
    story: "Search and schema lookups are filtered per caller, so agents cannot even learn that forbidden tools exist.",
    run: async ({ log }) => {
      const s = await mcp(key("rider-bot"), "/mcp", "tools/call", { name: "search_tools", arguments: { query: "refund a payment", limit: 10 } }, "rider-bot");
      const names: string[] = toolPayload(s.result).results.map((h: any) => h.name);
      log({ label: "rider-bot searches “refund a payment”", ok: !names.includes("payments__refund_payment"), detail: `results: ${names.join(", ") || "none"}` });
      const s2 = await mcp(key("care-copilot"), "/mcp", "tools/call", { name: "search_tools", arguments: { query: "refund a payment", limit: 3 } }, "care-copilot");
      const n2: string[] = toolPayload(s2.result).results.map((h: any) => h.name);
      log({ label: "care-copilot runs the same search", ok: n2[0] === "payments__refund_payment", detail: `top result: ${n2[0]}` });
      const forbidden = await call("rider-bot", "payments__refund_payment", {});
      const unknown = await call("rider-bot", "payments__does_not_exist", {});
      const same = forbidden.text.replace("refund_payment", "X") === unknown.text.replace("does_not_exist", "X");
      log({ label: "Forbidden and non-existent tools return identical errors (no existence oracle)", ok: same, detail: `${forbidden.text}  |  ${unknown.text}` });
    },
  },
  {
    id: "ratelimit",
    title: "Per-tool rate limiting",
    story: "The rider role may charge at most 5/min with a burst of 2. A rapid burst gets throttled with a retry-after hint.",
    run: async ({ log }) => {
      let limited = 0;
      for (let i = 1; i <= 5; i++) {
        const r = await call("rider-bot", "payments__charge_payment", { user_id: "u1", amount_usd: 1, method_id: "pm_wallet" });
        const isLimited = r.isError && r.text.includes("rate_limited");
        if (isLimited) limited++;
        const retry = r.meta?.["mini-mcp-gateway/error"]?.retry_after_s;
        log({ label: `charge #${i}`, ok: true, detail: isLimited ? `throttled — retry after ${retry}s` : r.isError ? r.text : `charged ${r.payload.charge_id}` });
      }
      log({ label: "Burst was throttled", ok: limited >= 3, detail: `${limited} of 5 calls rate-limited` });
    },
  },
  {
    id: "validation",
    title: "Schema validation at the edge",
    story: "Arguments are validated against the tool's JSON Schema before anything reaches the upstream.",
    run: async ({ log }) => {
      const bad = await call("rider-bot", "maps__geocode", { address: 42, extra_field: true });
      log({ label: "geocode with a number + unknown field → rejected by the gateway", ok: bad.isError && bad.text.includes("invalid_arguments"), detail: bad.text });
      const good = await call("rider-bot", "maps__geocode", { address: "ferry building" });
      log({ label: "valid arguments → reaches the legacy REST backend", ok: !good.isError, detail: good.isError ? good.text : JSON.stringify(good.payload) });
      const app = await call("rider-bot", "rides__get_ride_status", { ride_id: "ride_does_not_exist" });
      log({ label: "upstream business error is passed back as a tool error (not a gateway failure)", ok: app.isError && !app.text.startsWith("["), detail: app.text });
    },
  },
  {
    id: "circuit",
    title: "Circuit breaker on a failing upstream",
    story: "A server that is down trips the breaker after repeated failures; the gateway then fails fast instead of piling up timeouts.",
    run: async ({ log }) => {
      const admin = key("ops-admin");
      const existing = (await http(admin, "/admin/servers")).servers.some((s: any) => s.name === "flaky-svc");
      if (existing) await http(admin, "/admin/servers/flaky-svc", { method: "DELETE" });
      await http(admin, "/admin/servers", { method: "POST", body: JSON.stringify({
        name: "flaky-svc", kind: "rest", url: "http://127.0.0.1:1", timeout_s: 2, description: "Always down",
        operations: [{ name: "ping", path: "/ping", description: "Ping a service that is down" }] }) });
      log({ label: "Registered flaky-svc (nothing listens on its port)", ok: true, detail: "POST /admin/servers" });
      try {
        for (let i = 1; i <= 6; i++) {
          const t0 = performance.now();
          const r = await call("ops-admin", "flaky-svc__ping", {});
          const ms = Math.round(performance.now() - t0);
          const open = r.text.includes("circuit open");
          log({ label: `call #${i}`, ok: r.isError, detail: `${open ? "FAST-FAIL (circuit open)" : "upstream failure"} in ${ms} ms — ${r.text}` });
        }
        const sv = (await http(admin, "/admin/servers")).servers.find((s: any) => s.name === "flaky-svc");
        log({ label: "Registry shows the circuit as open", ok: sv?.circuit === "open", detail: `circuit=${sv?.circuit}` });
      } finally {
        const cleaned = await http(admin, "/admin/servers/flaky-svc", { method: "DELETE" }).then(() => true, () => false);
        log({ label: "Cleaned up flaky-svc", ok: cleaned, detail: "DELETE /admin/servers/flaky-svc" });
      }
    },
  },
  {
    id: "auth",
    title: "Authentication",
    story: "Requests without a valid key never reach the gateway logic; failures are counted and audited.",
    run: async ({ log }) => {
      for (const [label, k] of [["no key", null], ["wrong key", "not-a-real-key"]] as const) {
        try {
          await http(k, "/v1/whoami");
          log({ label: `${label} → should be rejected`, ok: false, detail: "request was accepted!" });
        } catch (e) {
          log({ label: `${label} → 401`, ok: e instanceof ApiError && e.status === 401, detail: (e as Error).message });
        }
      }
      const me = await http(key("rider-bot"), "/v1/whoami");
      log({ label: "valid key → principal resolved", ok: me.id === "rider-bot", detail: JSON.stringify(me) });
    },
  },
];

export function Scenarios() {
  const [results, setResults] = useState<Record<string, { steps: Step[]; running: boolean; error?: string }>>({});

  const run = async (s: (typeof SCENARIOS)[number]) => {
    setResults((r) => ({ ...r, [s.id]: { steps: [], running: true } }));
    const log = (step: Step) => setResults((r) => ({ ...r, [s.id]: { ...r[s.id], steps: [...(r[s.id]?.steps ?? []), step] } }));
    try {
      await s.run({ log });
      setResults((r) => ({ ...r, [s.id]: { ...r[s.id], running: false } }));
    } catch (e: any) {
      setResults((r) => ({ ...r, [s.id]: { ...r[s.id], running: false, error: e.message } }));
    }
  };

  const runAll = async () => { for (const s of SCENARIOS) await run(s); };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Guided scenarios</h1>
          <p className="muted">One-click demonstrations of the gateway's guarantees. Each runs real requests and checks the outcome.</p>
        </div>
        <button className="btn btn-primary" onClick={runAll}>Run all</button>
      </div>
      <div className="scenarios">
        {SCENARIOS.map((s) => {
          const r = results[s.id];
          const passed = r && !r.running && !r.error && r.steps.every((x) => x.ok);
          return (
            <Card key={s.id} title={s.title}
                  actions={<>
                    {r && !r.running && (passed ? <Badge tone="ok">PASS</Badge> : <Badge tone="bad">FAIL</Badge>)}
                    <button className="btn btn-ghost btn-sm" disabled={r?.running} onClick={() => run(s)}>{r?.running ? "Running…" : r ? "Run again" : "Run"}</button>
                  </>}>
              <p className="muted">{s.story}</p>
              {r && (
                <ol className="steps">
                  {r.steps.map((st, i) => (
                    <li key={i} className={st.ok ? "ok" : "bad"}>
                      <span className="step-icon">{st.ok ? "✓" : "✕"}</span>
                      <div><div>{st.label}</div><div className="muted small mono">{st.detail}</div></div>
                    </li>
                  ))}
                  {r.running && <li className="pending"><span className="step-icon spin">◌</span><div>running…</div></li>}
                  {r.error && <li className="bad"><span className="step-icon">✕</span><div>{r.error}</div></li>}
                </ol>
              )}
            </Card>
          );
        })}
      </div>
    </div>
  );
}
