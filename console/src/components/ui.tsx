import { useMemo, useState, type ReactNode } from "react";

function escapeHtml(s: string) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** Syntax-highlighted JSON. */
export function JsonView({ value, maxHeight }: { value: unknown; maxHeight?: number }) {
  const html = useMemo(() => {
    const text = typeof value === "string" ? value : JSON.stringify(value, null, 2) ?? "null";
    return escapeHtml(text).replace(
      /("(\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g,
      (m) => {
        let cls = "j-num";
        if (m.startsWith('"')) cls = m.endsWith(":") ? "j-key" : "j-str";
        else if (/true|false/.test(m)) cls = "j-bool";
        else if (m === "null") cls = "j-null";
        return `<span class="${cls}">${m}</span>`;
      },
    );
  }, [value]);
  return <pre className="json" style={{ maxHeight }} dangerouslySetInnerHTML={{ __html: html }} />;
}

export function Badge({ tone = "neutral", children, title }: { tone?: string; children: ReactNode; title?: string }) {
  return <span className={`badge badge-${tone}`} title={title}>{children}</span>;
}

export function Dot({ ok, pulse }: { ok: boolean | null; pulse?: boolean }) {
  return <span className={`dot ${ok == null ? "dot-unknown" : ok ? "dot-ok" : "dot-bad"} ${pulse ? "pulse" : ""}`} />;
}

export function Card({ title, actions, children, className = "" }: {
  title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card-head">
          <h3>{title}</h3>
          <div className="card-actions">{actions}</div>
        </header>
      )}
      {children}
    </section>
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: string }) {
  return (
    <div className={`stat ${tone ? `stat-${tone}` : ""}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="error-note">⚠ {error}</div>;
}

export function Collapsible({ summary, children, defaultOpen = false }: {
  summary: ReactNode; children: ReactNode; defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={`collapsible ${open ? "open" : ""}`}>
      <button className="collapsible-head" onClick={() => setOpen(!open)}>
        <span className="chev">{open ? "▾" : "▸"}</span>
        {summary}
      </button>
      {open && <div className="collapsible-body">{children}</div>}
    </div>
  );
}

export const KIND_LABEL: Record<string, string> = {
  mcp_http: "MCP · HTTP", mcp_stdio: "MCP · stdio", openapi: "REST · OpenAPI", rest: "REST · declared",
};

export function outcomeTone(outcome?: string): string {
  if (!outcome) return "neutral";
  if (outcome === "ok") return "ok";
  if (outcome === "tool_error") return "warn";
  if (outcome === "rate_limited") return "warn";
  return "bad";
}

export function timeAgo(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}
