import results from "../../../experiments/results.json";
import { HBarCompare, LogBars } from "../components/charts";
import { Card, Stat } from "../components/ui";

export function Experiment() {
  const live = results.live;
  const tasks = live.tasks;
  const fullSum = tasks.reduce((a, t) => a + t.full_total, 0);
  const incrSum = tasks.reduce((a, t) => a + t.incr_total, 0);
  const reduction = 100 * (1 - live.meta_definitions_tokens / live.full_definitions_tokens);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Experiment: full vs incremental discovery</h1>
          <p className="muted">
            Measured against the live gateway with {live.tool_count} tools across 5 upstreams
            (<code>python -m experiments.token_usage</code>). Tokenizer: tiktoken <code>o200k_base</code> — a provider-neutral proxy.
          </p>
        </div>
      </div>

      <div className="stats">
        <Stat label="Full catalog definitions" value={live.full_definitions_tokens.toLocaleString()} sub="tokens on every model request" />
        <Stat label="Incremental meta-tools" value={live.meta_definitions_tokens.toLocaleString()} sub="tokens on every model request" tone="ok" />
        <Stat label="Fixed overhead cut" value={`${reduction.toFixed(0)}%`} tone="ok" sub="per request" />
        <Stat label="10 tasks, end to end" value={`${((100 * (incrSum - fullSum)) / fullSum).toFixed(0)}%`}
              sub={`${fullSum.toLocaleString()} → ${incrSum.toLocaleString()} tokens`} tone="ok" />
      </div>

      <Card title="Per task — tool-definition + discovery tokens over the whole agent loop">
        <HBarCompare aLabel="Full discovery" bLabel="Incremental discovery"
                     rows={tasks.map((t) => ({ label: t.task, a: t.full_total, b: t.incr_total, note: `${t.tools_needed} tool${t.tools_needed > 1 ? "s" : ""}` }))} />
        <p className="muted small">
          Incremental wins on single-tool tasks but can cost more on multi-tool tasks at only 20 tools, because each newly needed tool costs a
          search and a schema fetch while the full catalog is still small. Search recall@5 was 100% on all 14 queries.
        </p>
      </Card>

      <div className="grid-2">
        <Card title="Scaling the catalog — tokens per request (log scale)">
          <LogBars rows={results.scale.flatMap((r) => [
            { label: `${r.catalog_size.toLocaleString()} tools · full`, value: r.full_definitions_tokens, tone: "a" as const },
            { label: `${r.catalog_size.toLocaleString()} tools · incremental`, value: live.meta_definitions_tokens, tone: "b" as const },
          ])} />
        </Card>
        <Card title="Does search still find the right tool?">
          <table className="table">
            <thead><tr><th>Catalog size</th><th>Recall@1</th><th>Recall@5</th></tr></thead>
            <tbody>
              {results.scale.map((r) => (
                <tr key={r.catalog_size}>
                  <td>{r.catalog_size.toLocaleString()}</td>
                  <td>{(r.recall_at_1 * 100).toFixed(0)}%</td>
                  <td>{(r.recall_at_5 * 100).toFixed(0)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">
            Half of the synthetic distractors reuse the real tools' vocabulary (ride, fare, order, invoice…). Top-1 drops as they are added while
            top-5 holds — which is why <code>search_tools</code> returns several candidates. Synthetic distractors are a proxy, not a real catalog.
          </p>
        </Card>
      </div>

      <Card title="Trade-offs">
        <ul className="bullets">
          <li><b>Fixed cost</b> — full discovery resends every definition on every model call and grows linearly; incremental is constant.</li>
          <li><b>Latency</b> — incremental adds two model round-trips per newly needed tool. Pin hot tools and search the long tail; <code>/mcp</code> also accepts direct calls by qualified name.</li>
          <li><b>Prompt caching</b> lowers the $ cost of a static tool prefix, but not the context window it occupies or the selection-accuracy cost of thousands of tools.</li>
          <li><b>Search quality</b> is the main risk; the BM25 ranker is a baseline behind a pluggable interface for embedding/hybrid retrieval.</li>
        </ul>
      </Card>
    </div>
  );
}
