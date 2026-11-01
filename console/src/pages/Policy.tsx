import { http } from "../api";
import { Badge, Card, ErrorNote } from "../components/ui";
import { usePoll } from "../hooks";

export function Policy({ adminKey }: { adminKey: string }) {
  const m = usePoll(() => http(adminKey, "/admin/policy/matrix"), 10000, [adminKey]);
  const principals: any[] = m.data?.principals ?? [];
  const rows: any[] = m.data?.tools ?? [];
  const counts = Object.fromEntries(principals.map((p) => [p.id, rows.filter((r) => r.access[p.id].allowed).length]));

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Access policy</h1>
          <p className="muted">
            Effective RBAC for every configured principal × tool, evaluated by the live policy engine.
            Rules: glob allow/deny per role, <b>deny wins</b>, read-only roles only get <code>readOnlyHint</code> tools. Hover a cell for the reason.
          </p>
        </div>
      </div>
      <ErrorNote error={m.error} />
      <Card>
        <div className="matrix-wrap">
          <table className="matrix">
            <thead>
              <tr>
                <th>Tool</th>
                {principals.map((p) => (
                  <th key={p.id}><div>{p.id}</div><div className="muted small">{p.roles.join(", ")}</div>
                    <div className="small">{counts[p.id]}/{rows.length}</div></th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r.tool} className={i > 0 && rows[i - 1].server !== r.server ? "group-start" : ""}>
                  <td><code>{r.tool}</code> {r.read_only ? <Badge tone="info">ro</Badge> : <Badge tone="warn">rw</Badge>}</td>
                  {principals.map((p) => {
                    const a = r.access[p.id];
                    return (
                      <td key={p.id} className={`cell ${a.allowed ? "allow" : "deny"}`} title={a.reason}>
                        {a.allowed ? "✓" : "✕"}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
