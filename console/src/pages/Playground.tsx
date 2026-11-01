import { useCallback, useEffect, useState } from "react";
import { approxTokens, DEMO_IDENTITIES, mcp, toolPayload, toolText, type Identity, type WireEntry } from "../api";
import { coerceArgs, harvestIds, initialValues, SchemaForm, type Json } from "../components/SchemaForm";
import { Badge, Card, Empty, JsonView } from "../components/ui";
import { WireInspector } from "../components/WireInspector";

const EXAMPLES = [
  "how much does a ride to the airport cost",
  "book a ride",
  "order sushi for dinner",
  "refund a charge",
  "where is my food order",
  "complain about my driver",
  "driving time to the ferry building",
];

interface Hit { name: string; summary: string; required_args: string[]; read_only: boolean; score: number }
interface ToolSchema { name: string; description: string; input_schema: Json; annotations?: Json }
interface CallOutcome { isError: boolean; payload: any; text: string; ms: number; requestId: string | null; meta?: Json }

export function Playground({ wire }: { wire: { entries: WireEntry[]; clear: () => void } }) {
  const [identity, setIdentity] = useState<Identity>(DEMO_IDENTITIES[0]);
  const [mode, setMode] = useState<"search" | "full">("search");
  const [ctx, setCtx] = useState<{ fullBytes: number; fullCount: number; metaBytes: number } | null>(null);
  const [discoveryBytes, setDiscoveryBytes] = useState(0);
  const [fullTools, setFullTools] = useState<ToolSchema[]>([]);
  const [filter, setFilter] = useState("");

  const [query, setQuery] = useState(EXAMPLES[0]);
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [tool, setTool] = useState<ToolSchema | null>(null);
  const [values, setValues] = useState<Json>({});
  const [memory, setMemory] = useState<Json>({});
  const [outcome, setOutcome] = useState<CallOutcome | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const who = identity.id;

  // Measure what each discovery mode would load into the model's context for this identity.
  useEffect(() => {
    let alive = true;
    setHits(null); setTool(null); setOutcome(null); setDiscoveryBytes(0); setError(null);
    (async () => {
      const [meta, full] = await Promise.all([
        mcp(identity.key, "/mcp", "tools/list", {}, who),
        mcp(identity.key, "/mcp/full", "tools/list", {}, who),
      ]);
      if (!alive) return;
      const tools: ToolSchema[] = (full.result?.tools ?? []).map((t: any) => ({
        name: t.name, description: t.description, input_schema: t.inputSchema, annotations: t.annotations,
      }));
      setFullTools(tools);
      setCtx({ fullBytes: full.bytes, fullCount: tools.length, metaBytes: meta.bytes });
    })().catch((e) => alive && setError(String(e)));
    return () => { alive = false; };
  }, [identity, who]);

  const search = useCallback(async (q: string) => {
    setBusy("search"); setError(null); setTool(null); setOutcome(null);
    try {
      const r = await mcp(identity.key, "/mcp", "tools/call", { name: "search_tools", arguments: { query: q, limit: 5 } }, who);
      if (r.error || r.result?.isError) throw new Error(r.error?.message ?? toolText(r.result));
      setHits(toolPayload(r.result).results);
      setDiscoveryBytes((b) => b + r.bytes);
    } catch (e: any) { setError(e.message); } finally { setBusy(null); }
  }, [identity, who]);

  const describe = async (name: string) => {
    setBusy("describe"); setError(null); setOutcome(null);
    try {
      const r = await mcp(identity.key, "/mcp", "tools/call", { name: "get_tool_schema", arguments: { names: [name] } }, who);
      const t: ToolSchema | undefined = toolPayload(r.result)?.tools?.[0];
      if (!t) throw new Error(`${name}: not found or not permitted`);
      setDiscoveryBytes((b) => b + r.bytes);
      selectTool(t);
    } catch (e: any) { setError(e.message); } finally { setBusy(null); }
  };

  const selectTool = (t: ToolSchema) => {
    setTool(t);
    setValues(initialValues(t.name, t.input_schema, memory));
    setOutcome(null);
  };

  const call = async () => {
    if (!tool) return;
    setBusy("call"); setError(null);
    try {
      const args = coerceArgs(tool.input_schema, values);
      const r = mode === "search"
        ? await mcp(identity.key, "/mcp", "tools/call", { name: "call_tool", arguments: { name: tool.name, arguments: args } }, who)
        : await mcp(identity.key, "/mcp/full", "tools/call", { name: tool.name, arguments: args }, who);
      if (r.error) throw new Error(`${r.status}: ${r.error.message}`);
      const payload = toolPayload(r.result);
      setOutcome({ isError: !!r.result?.isError, payload, text: toolText(r.result), ms: r.ms, requestId: r.requestId,
                   meta: r.result?._meta });
      if (!r.result?.isError) setMemory((m) => harvestIds(payload, m));
    } catch (e: any) { setError(e.message); } finally { setBusy(null); }
  };

  const filtered = fullTools.filter((t) => !filter || t.name.includes(filter.toLowerCase()) || t.description.toLowerCase().includes(filter.toLowerCase()));
  const incrBytes = (ctx?.metaBytes ?? 0) + discoveryBytes;

  return (
    <div className="page playground">
      <div className="page-head">
        <div>
          <h1>Agent Playground</h1>
          <p className="muted">Act as an agent. Every step below is a real MCP request to the gateway — inspect the wire traffic on the right.</p>
        </div>
      </div>

      <div className="identity-row">
        {DEMO_IDENTITIES.map((i) => (
          <button key={i.id} className={`identity ${identity.id === i.id ? "active" : ""}`} onClick={() => setIdentity(i)}>
            <div className="identity-top"><strong>{i.label}</strong><Badge>{i.role}</Badge></div>
            <div className="identity-blurb">{i.blurb}</div>
          </button>
        ))}
      </div>

      <div className="pg-grid">
        <div className="pg-main">
          <Card title="Discovery mode" className="mode-card">
            <div className="segmented">
              <button className={mode === "search" ? "active" : ""} onClick={() => { setMode("search"); setTool(null); setOutcome(null); }}>
                Incremental <span className="muted">/mcp</span>
              </button>
              <button className={mode === "full" ? "active" : ""} onClick={() => { setMode("full"); setTool(null); setOutcome(null); }}>
                Full catalog <span className="muted">/mcp/full</span>
              </button>
            </div>
            <ContextMeter ctx={ctx} incrBytes={incrBytes} discoveryBytes={discoveryBytes} mode={mode} />
          </Card>

          {error && <div className="error-note">⚠ {error}</div>}

          {mode === "search" ? (
            <Card title={<><span className="step">1</span> Search for a capability <code>search_tools</code></>}>
              <form className="search-row" onSubmit={(e) => { e.preventDefault(); search(query); }}>
                <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Describe what the agent needs to do…" />
                <button className="btn btn-primary" disabled={!query.trim() || busy === "search"}>{busy === "search" ? "Searching…" : "Search"}</button>
              </form>
              <div className="chips">
                {EXAMPLES.map((ex) => <button key={ex} className="chip" onClick={() => { setQuery(ex); search(ex); }}>{ex}</button>)}
              </div>
              {hits && (hits.length ? (
                <ul className="hits">
                  {hits.map((h) => (
                    <li key={h.name} className={`hit ${tool?.name === h.name ? "active" : ""}`} onClick={() => describe(h.name)}>
                      <div className="hit-top">
                        <code>{h.name}</code>
                        {h.read_only ? <Badge tone="info">read-only</Badge> : <Badge tone="warn">write</Badge>}
                        <span className="score"><span style={{ width: `${Math.min(100, (h.score / hits[0].score) * 100)}%` }} /></span>
                        <span className="muted small">{h.score.toFixed(2)}</span>
                      </div>
                      <div className="hit-summary">{h.summary}</div>
                    </li>
                  ))}
                </ul>
              ) : <Empty>No tools this identity may use match that query.</Empty>)}
              {hits && <p className="muted small">Only tools <b>{identity.label}</b> is permitted to call are searchable. Click a result to fetch its schema.</p>}
            </Card>
          ) : (
            <Card title={<><span className="step">1</span> Pick from the full catalog <code>tools/list</code></>}
                  actions={<input className="input-sm" placeholder="filter" value={filter} onChange={(e) => setFilter(e.target.value)} />}>
              <p className="muted small">The model would receive all {fullTools.length} definitions below on every request.</p>
              <ul className="tool-grid">
                {filtered.map((t) => (
                  <li key={t.name} className={`tool-chip ${tool?.name === t.name ? "active" : ""}`} onClick={() => selectTool(t)}>
                    <code>{t.name}</code>
                  </li>
                ))}
              </ul>
            </Card>
          )}

          {tool && (
            <Card title={<><span className="step">2</span> {mode === "search" ? <>Schema <code>get_tool_schema</code></> : "Arguments"}</>}>
              <div className="tool-head">
                <code className="tool-name">{tool.name}</code>
                {tool.annotations?.readOnlyHint ? <Badge tone="info">read-only</Badge> : <Badge tone="warn">write</Badge>}
                {tool.annotations?.destructiveHint && !tool.annotations?.readOnlyHint && <Badge tone="bad">destructive</Badge>}
              </div>
              <p className="tool-desc">{tool.description}</p>
              <SchemaForm schema={tool.input_schema} values={values} onChange={setValues} />
              {Object.keys(memory).length > 0 && (
                <p className="muted small">Remembered from earlier results: {Object.entries(memory).map(([k, v]) => <code key={k} className="mem">{k}={String(v)}</code>)}</p>
              )}
              <div className="row-end">
                <button className="btn btn-primary" onClick={call} disabled={busy === "call"}>
                  {busy === "call" ? "Calling…" : <>Call via gateway <span className="muted">→ {mode === "search" ? "call_tool" : "tools/call"}</span></>}
                </button>
              </div>
            </Card>
          )}

          {outcome && (
            <Card title={<><span className="step">3</span> Result</>}
                  actions={<>
                    {outcome.isError ? <Badge tone="bad">isError</Badge> : <Badge tone="ok">success</Badge>}
                    <Badge>{outcome.ms} ms</Badge>
                  </>}>
              {outcome.isError && <div className="callout callout-bad">{outcome.text}</div>}
              {!outcome.isError && <JsonView value={outcome.payload} maxHeight={320} />}
              {outcome.requestId && <p className="muted small">Request ID <code>{outcome.requestId}</code> — find it in the Audit log.</p>}
            </Card>
          )}
        </div>

        <aside className="pg-side">
          <WireInspector entries={wire.entries} onClear={wire.clear} />
        </aside>
      </div>
    </div>
  );
}

function ContextMeter({ ctx, incrBytes, discoveryBytes, mode }: {
  ctx: { fullBytes: number; fullCount: number; metaBytes: number } | null; incrBytes: number; discoveryBytes: number; mode: string;
}) {
  if (!ctx) return <div className="muted small">Measuring…</div>;
  const max = Math.max(ctx.fullBytes, incrBytes, 1);
  return (
    <div className="ctx">
      <div className="ctx-title">Tool context the model carries <span className="muted small">(measured from real responses · ≈4 bytes/token)</span></div>
      <div className={`ctx-row ${mode === "full" ? "on" : ""}`}>
        <span className="ctx-label">Full catalog · {ctx.fullCount} tools</span>
        <div className="hbar-track"><div className="hbar-a" style={{ width: `${(100 * ctx.fullBytes) / max}%` }} />
          <span>≈{approxTokens(ctx.fullBytes).toLocaleString()} tok · every request</span></div>
      </div>
      <div className={`ctx-row ${mode === "search" ? "on" : ""}`}>
        <span className="ctx-label">Incremental · 3 meta-tools + discovery</span>
        <div className="hbar-track">
          <div className="hbar-b" style={{ width: `${(100 * ctx.metaBytes) / max}%` }} />
          <div className="hbar-b2" style={{ width: `${(100 * discoveryBytes) / max}%` }} />
          <span>≈{approxTokens(ctx.metaBytes).toLocaleString()} + {approxTokens(discoveryBytes).toLocaleString()} tok</span>
        </div>
      </div>
    </div>
  );
}
