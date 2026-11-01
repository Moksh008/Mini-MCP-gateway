import { useRef, useState } from "react";
import { DEMO_IDENTITIES, type Identity } from "../api";
import { Badge, Card, Collapsible, JsonView } from "../components/ui";

const TASKS = [
  "How much would an economy ride from 1455 Market St to SFO airport cost?",
  "Book me a comfort ride from the Ferry Building to SFO and tell me the driver ETA.",
  "Order a salmon roll and a miso soup from Sakura Sushi and tell me the total.",
  "I left my phone in my last ride. Please open a support ticket.",
  "Refund $5 of my last charge — the driver took a longer route.",
  "How long does it take to drive from 1455 Market St to the Ferry Building?",
];

type AgentEvent = Record<string, any> & { type: string };

interface RunRecord {
  id: number;
  task: string;
  mode: string;
  model: string;
  identity: string;
  events: AgentEvent[];
  status: "running" | "done" | "error";
  totals?: Record<string, any>;
  toolDefBytes?: number;
}

export interface AgentInfo {
  available: boolean;
  reason?: string | null;
  default_model?: string;
  models?: string[];
  max_steps?: number;
  daily_runs_left?: number;
}

let runSeq = 0;

export function AgentPage({ info }: { info: AgentInfo | null }) {
  const [identity, setIdentity] = useState<Identity>(DEMO_IDENTITIES[0]);
  const [mode, setMode] = useState<"search" | "full">("search");
  const [model, setModel] = useState<string>("");
  const [task, setTask] = useState(TASKS[0]);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const running = runs.some((r) => r.status === "running");
  const active = runs.find((r) => r.id === activeId) ?? runs[0];
  const chosenModel = model || info?.default_model || "";

  const update = (id: number, fn: (r: RunRecord) => RunRecord) => setRuns((rs) => rs.map((r) => (r.id === id ? fn(r) : r)));

  const run = async (runMode: "search" | "full" = mode, runTask = task) => {
    const id = ++runSeq;
    const record: RunRecord = { id, task: runTask, mode: runMode, model: chosenModel, identity: identity.id, events: [], status: "running" };
    setRuns((rs) => [record, ...rs]);
    setActiveId(id);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await fetch("/v1/agent/run", {
        method: "POST", signal: ctrl.signal,
        headers: { Authorization: `Bearer ${identity.key}`, "content-type": "application/json" },
        body: JSON.stringify({ task: runTask, mode: runMode, model: chosenModel || undefined }),
      });
      if (!res.ok || !res.body) {
        const body = await res.json().catch(() => ({ message: res.statusText }));
        update(id, (r) => ({ ...r, status: "error", events: [{ type: "error", message: body.message ?? `HTTP ${res.status}` }] }));
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
          const line = frame.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;
          const ev: AgentEvent = JSON.parse(line.slice(6));
          update(id, (r) => ({
            ...r,
            events: [...r.events, ev],
            toolDefBytes: ev.type === "start" ? ev.tool_definition_bytes : r.toolDefBytes,
            status: ev.type === "final" ? "done" : ev.type === "error" ? "error" : r.status,
            totals: ev.totals ?? r.totals,
          }));
        }
      }
      update(id, (r) => (r.status === "running" ? { ...r, status: "error", events: [...r.events, { type: "error", message: "stream ended" }] } : r));
    } catch (e: any) {
      update(id, (r) => ({ ...r, status: "error", events: [...r.events, { type: "error", message: e.name === "AbortError" ? "stopped" : e.message }] }));
    }
  };

  if (info && !info.available) {
    return (
      <div className="page">
        <div className="page-head"><div><h1>AI Agent</h1></div></div>
        <Card title="The agent is not enabled on this gateway">
          <p className="muted">{info.reason}. Add an OpenRouter key and restart the gateway:</p>
          <JsonView value={"# .env (next to docker-compose.yml)\nOPENROUTER_API_KEY=sk-or-...\n\ndocker compose up -d"} />
        </Card>
      </div>
    );
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>AI Agent</h1>
          <p className="muted">
            A real LLM completes the task using <b>only</b> the gateway — as the identity you pick, so RBAC, rate limits and audit apply.
            Token counts come from the provider. Run the same task in both discovery modes to compare.
          </p>
        </div>
      </div>

      <Card>
        <div className="agent-controls">
          <div className="agent-row">
            <label className="label">Act as</label>
            <div className="segmented">
              {DEMO_IDENTITIES.map((i) => (
                <button key={i.id} className={identity.id === i.id ? "active" : ""} onClick={() => setIdentity(i)} title={i.blurb}>{i.label}</button>
              ))}
            </div>
          </div>
          <div className="agent-row">
            <label className="label">Discovery</label>
            <div className="segmented">
              <button className={mode === "search" ? "active" : ""} onClick={() => setMode("search")}>Incremental · 3 meta-tools</button>
              <button className={mode === "full" ? "active" : ""} onClick={() => setMode("full")}>Full catalog</button>
            </div>
            <label className="label">Model</label>
            <select value={chosenModel} onChange={(e) => setModel(e.target.value)} style={{ maxWidth: 260 }}>
              {(info?.models ?? []).map((m) => <option key={m}>{m}</option>)}
            </select>
          </div>
          <textarea rows={2} value={task} onChange={(e) => setTask(e.target.value)} maxLength={1000} />
          <div className="chips">{TASKS.map((t) => <button key={t} className="chip" onClick={() => setTask(t)}>{t}</button>)}</div>
          <div className="row-end" style={{ marginTop: 4 }}>
            {info?.daily_runs_left != null && <span className="muted small" style={{ marginRight: "auto" }}>{info.daily_runs_left} runs left today on this demo</span>}
            {running && <button className="btn btn-ghost" onClick={() => abortRef.current?.abort()}>Stop</button>}
            <button className="btn btn-primary" disabled={running || task.trim().length < 3} onClick={() => run()}>
              {running ? "Agent working…" : "Run agent"}
            </button>
          </div>
        </div>
      </Card>

      {runs.length > 1 && <CompareTable runs={runs} activeId={active?.id} onSelect={setActiveId} />}

      {active && (
        <Card title={<>Run <span className="muted">· {active.identity} · {active.mode === "search" ? "incremental" : "full catalog"} · {active.model}</span></>}
              actions={<>
                {active.status === "running" && <Badge tone="info">running</Badge>}
                {active.status === "done" && <Badge tone="ok">done</Badge>}
                {active.status === "error" && <Badge tone="bad">error</Badge>}
                {active.status !== "running" && (
                  <button className="btn btn-ghost btn-sm" disabled={running}
                          onClick={() => run(active.mode === "search" ? "full" : "search", active.task)}>
                    Re-run in {active.mode === "search" ? "full catalog" : "incremental"} mode
                  </button>
                )}
              </>}>
          <div className="agent-task">“{active.task}”</div>
          <Timeline run={active} />
        </Card>
      )}
    </div>
  );
}

function Timeline({ run }: { run: RunRecord }) {
  return (
    <ol className="timeline">
      {run.events.map((ev, i) => {
        if (ev.type === "start") {
          return (
            <li key={i} className="tl tl-start">
              <span className="tl-dot" />
              <div>Offered the model <b>{ev.tools_offered}</b> tool definition{ev.tools_offered === 1 ? "" : "s"}
                <span className="muted"> ({(ev.tool_definition_bytes / 1024).toFixed(1)} KB, resent on every LLM call)</span></div>
            </li>
          );
        }
        if (ev.type === "llm") {
          return (
            <li key={i} className="tl tl-llm">
              <span className="tl-dot" />
              <div className="tl-body">
                <div className="tl-head">
                  <b>LLM call {ev.step}</b>
                  <Badge tone="info">{ev.prompt_tokens?.toLocaleString() ?? "?"} in</Badge>
                  <Badge>{ev.completion_tokens ?? "?"} out</Badge>
                  <span className="muted small">{ev.ms} ms</span>
                </div>
                {ev.text && <div className="tl-text">{ev.text}</div>}
                {ev.tool_calls?.length > 0 && <div className="muted small">→ requests {ev.tool_calls.map((t: string) => <code key={t + i}>{t}</code>)}</div>}
              </div>
            </li>
          );
        }
        if (ev.type === "tool") {
          return (
            <li key={i} className={`tl tl-tool ${ev.is_error ? "err" : ""}`}>
              <span className="tl-dot" />
              <div className="tl-body">
                <Collapsible summary={
                  <span className="tl-head">
                    <code>{ev.name}</code>
                    {ev.target && <>→ <code>{ev.target}</code></>}
                    {ev.is_error ? <Badge tone="bad">error</Badge> : <Badge tone="ok">ok</Badge>}
                    <span className="muted small">{ev.ms} ms via gateway</span>
                  </span>
                }>
                  <div className="label">Arguments</div>
                  <JsonView value={ev.arguments} maxHeight={200} />
                  <div className="label">Result (as the model saw it)</div>
                  <JsonView value={tryParse(ev.result)} maxHeight={240} />
                </Collapsible>
                {ev.is_error && <div className="tl-err mono small">{ev.result}</div>}
              </div>
            </li>
          );
        }
        if (ev.type === "final") {
          return (
            <li key={i} className="tl tl-final">
              <span className="tl-dot" />
              <div className="tl-body">
                <div className="final-answer">{ev.text}</div>
                <Totals totals={ev.totals} />
              </div>
            </li>
          );
        }
        if (ev.type === "error") {
          return <li key={i} className="tl tl-tool err"><span className="tl-dot" /><div className="tl-err">{ev.message}</div></li>;
        }
        return null;
      })}
      {run.status === "running" && <li className="tl tl-pending"><span className="tl-dot spin-dot" /><div className="muted">thinking…</div></li>}
    </ol>
  );
}

function Totals({ totals }: { totals: Record<string, any> }) {
  return (
    <div className="totals">
      <span><b>{totals.prompt_tokens?.toLocaleString()}</b> input tokens</span>
      <span><b>{totals.completion_tokens?.toLocaleString()}</b> output tokens</span>
      <span><b>{totals.llm_calls}</b> LLM calls</span>
      <span><b>{totals.tool_calls}</b> gateway calls{totals.tool_errors ? ` (${totals.tool_errors} errors)` : ""}</span>
      {totals.cost_usd > 0 && <span><b>${totals.cost_usd.toFixed(5)}</b></span>}
      <span><b>{(totals.ms / 1000).toFixed(1)}s</b></span>
    </div>
  );
}

function CompareTable({ runs, activeId, onSelect }: { runs: RunRecord[]; activeId?: number; onSelect: (id: number) => void }) {
  return (
    <Card title="Runs — provider-reported token usage">
      <table className="table">
        <thead><tr><th>Task</th><th>Mode</th><th>Identity</th><th>Tool defs</th><th>Input tok</th><th>Output tok</th><th>LLM calls</th><th>Tools used</th><th>Time</th></tr></thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id} className={`clickable ${r.id === activeId ? "row-active" : ""}`} onClick={() => onSelect(r.id)}>
              <td className="ellipsis" style={{ maxWidth: 320 }}>{r.task}</td>
              <td><Badge tone={r.mode === "search" ? "ok" : "neutral"}>{r.mode === "search" ? "incremental" : "full"}</Badge></td>
              <td>{r.identity}</td>
              <td>{r.toolDefBytes ? `${(r.toolDefBytes / 1024).toFixed(1)} KB` : "—"}</td>
              <td><b>{r.totals?.prompt_tokens?.toLocaleString() ?? "…"}</b></td>
              <td>{r.totals?.completion_tokens?.toLocaleString() ?? "…"}</td>
              <td>{r.totals?.llm_calls ?? "…"}</td>
              <td className="small">{r.totals?.tools_used?.join(", ") || "—"}</td>
              <td>{r.totals ? `${(r.totals.ms / 1000).toFixed(1)}s` : "…"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function tryParse(text: string): unknown {
  try { return JSON.parse(text); } catch { return text; }
}
