import { useState } from "react";
import type { WireEntry } from "../api";
import { Badge, JsonView } from "./ui";

/** Live view of the raw MCP JSON-RPC traffic this browser exchanged with the gateway. */
export function WireInspector({ entries, onClear }: { entries: WireEntry[]; onClear: () => void }) {
  const [selected, setSelected] = useState<number | null>(null);
  const sel = entries.find((e) => e.seq === selected) ?? entries[0];

  return (
    <div className="wire">
      <div className="wire-head">
        <div>
          <strong>Wire inspector</strong>
          <span className="muted"> · real MCP JSON-RPC over Streamable HTTP</span>
        </div>
        <button className="btn btn-ghost btn-sm" onClick={onClear} disabled={!entries.length}>Clear</button>
      </div>
      {!entries.length ? (
        <div className="wire-empty">Requests you make will appear here, exactly as sent and received.</div>
      ) : (
        <>
          <ul className="wire-list">
            {entries.map((e) => (
              <li key={e.seq} className={`wire-row ${sel?.seq === e.seq ? "active" : ""}`} onClick={() => setSelected(e.seq)}>
                <span className={`wire-status ${e.ok ? "ok" : "bad"}`}>{e.status}</span>
                <span className="wire-method">{e.method}</span>
                <span className="wire-meta">{e.endpoint}</span>
                <span className="wire-meta">{e.ms} ms</span>
                <span className="wire-meta">{(e.bytes / 1024).toFixed(1)} KB</span>
              </li>
            ))}
          </ul>
          {sel && (
            <div className="wire-detail">
              <div className="wire-detail-meta">
                <Badge>{sel.identity}</Badge>
                <Badge tone="neutral">POST {sel.endpoint}</Badge>
                {sel.requestId && <Badge tone="info" title="X-Request-ID — also in the audit log">req {sel.requestId.slice(0, 12)}</Badge>}
              </div>
              <div className="wire-cols">
                <div>
                  <div className="label">Request</div>
                  <JsonView value={sel.request} maxHeight={260} />
                </div>
                <div>
                  <div className="label">Response</div>
                  <JsonView value={sel.response} maxHeight={260} />
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
