import { useMemo, useState } from "react";
import { http } from "../api";
import { Badge, Card, Empty, ErrorNote, JsonView } from "../components/ui";
import { usePoll } from "../hooks";

export function Catalog({ adminKey }: { adminKey: string }) {
  const catalog = usePoll(() => http(adminKey, "/admin/tools?limit=5000"), 8000, [adminKey]);
  const [q, setQ] = useState("");
  const [server, setServer] = useState("");
  const [selected, setSelected] = useState<string | null>(null);

  const tools: any[] = catalog.data?.tools ?? [];
  const servers = useMemo(() => [...new Set(tools.map((t) => t.name.split("__")[0]))].sort(), [tools]);
  const filtered = tools.filter((t) =>
    (!server || t.name.startsWith(server + "__")) &&
    (!q || t.name.toLowerCase().includes(q.toLowerCase()) || t.description.toLowerCase().includes(q.toLowerCase())));
  const sel = tools.find((t) => t.name === selected) ?? filtered[0];

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Tool catalog</h1>
          <p className="muted">The merged, namespaced catalog (<code>server__tool</code>) across every upstream — {tools.length} tools.</p>
        </div>
      </div>
      <ErrorNote error={catalog.error} />
      <div className="catalog">
        <Card className="catalog-list">
          <div className="filters">
            <input placeholder="Filter by name or description" value={q} onChange={(e) => setQ(e.target.value)} />
            <select value={server} onChange={(e) => setServer(e.target.value)}>
              <option value="">All servers</option>
              {servers.map((s) => <option key={s}>{s}</option>)}
            </select>
          </div>
          {filtered.length ? (
            <ul className="catalog-items">
              {filtered.map((t) => (
                <li key={t.name} className={sel?.name === t.name ? "active" : ""} onClick={() => setSelected(t.name)}>
                  <div><code>{t.name}</code></div>
                  <div className="muted small ellipsis">{t.description}</div>
                </li>
              ))}
            </ul>
          ) : <Empty>No tools match.</Empty>}
        </Card>
        {sel && (
          <Card className="catalog-detail" title={<code>{sel.name}</code>}
                actions={<>
                  {sel.annotations?.readOnlyHint ? <Badge tone="info">read-only</Badge> : <Badge tone="warn">write</Badge>}
                  {sel.annotations?.destructiveHint && !sel.annotations?.readOnlyHint && <Badge tone="bad">destructive</Badge>}
                  {sel.annotations?.idempotentHint && <Badge>idempotent</Badge>}
                </>}>
            <p>{sel.description}</p>
            <div className="label">Input schema</div>
            <JsonView value={sel.input_schema} maxHeight={420} />
            {sel.annotations && <><div className="label">Annotations</div><JsonView value={sel.annotations} /></>}
          </Card>
        )}
      </div>
    </div>
  );
}
