import { useEffect, useState } from "react";
import { DEMO_IDENTITIES, http } from "./api";
import { Dot } from "./components/ui";
import { usePoll, useSessionState, useWire } from "./hooks";
import { AgentPage } from "./pages/Agent";
import { Audit } from "./pages/Audit";
import { Catalog } from "./pages/Catalog";
import { Experiment } from "./pages/Experiment";
import { Overview, useTraffic } from "./pages/Overview";
import { Playground } from "./pages/Playground";
import { Policy } from "./pages/Policy";
import { Registry } from "./pages/Registry";
import { Scenarios } from "./pages/Scenarios";

const BACKEND_HOST = import.meta.env.VITE_BACKEND_HOST ?? "127.0.0.1";

const NAV = [
  { id: "overview", label: "Overview", icon: "◎", admin: true },
  { id: "agent", label: "AI Agent", icon: "✦", admin: false },
  { id: "playground", label: "Agent Playground", icon: "▶", admin: false },
  { id: "scenarios", label: "Scenarios", icon: "✓", admin: false },
  { id: "registry", label: "Registry", icon: "▤", admin: true },
  { id: "catalog", label: "Tool catalog", icon: "⌘", admin: true },
  { id: "policy", label: "Access policy", icon: "⛨", admin: true },
  { id: "audit", label: "Audit log", icon: "≡", admin: true },
  { id: "experiment", label: "Token experiment", icon: "◔", admin: false },
] as const;

type PageId = (typeof NAV)[number]["id"];

function currentPage(): PageId {
  const h = window.location.hash.replace("#/", "");
  return (NAV.find((n) => n.id === h)?.id ?? "overview") as PageId;
}

export function App() {
  const [page, setPage] = useState<PageId>(currentPage);
  const [adminKey, setAdminKey] = useSessionState<string | null>("mgw.adminKey", null);
  const wire = useWire();
  const traffic = useTraffic(adminKey);
  const health = usePoll(() => http(null, "/readyz"), 5000);
  const meta = usePoll(() => http(null, "/v1/meta"), 30000);

  useEffect(() => {
    const onHash = () => setPage(currentPage());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const navigate = (p: string) => { window.location.hash = `#/${p}`; };

  const nav = NAV.find((n) => n.id === page)!;
  const needsKey = nav.admin && !adminKey;

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <svg viewBox="0 0 32 32" width="28" height="28"><rect width="32" height="32" rx="8" fill="#12161c" />
            <path d="M8 10h16M8 16h16M8 22h16" stroke="#2ee59d" strokeWidth="2.5" strokeLinecap="round" />
            <circle cx="12" cy="10" r="2.5" fill="#2ee59d" /><circle cx="20" cy="16" r="2.5" fill="#2ee59d" /><circle cx="14" cy="22" r="2.5" fill="#2ee59d" /></svg>
          <div><div className="brand-name">MCP Gateway</div><div className="brand-sub">control console</div></div>
        </div>
        <nav>
          {NAV.map((n) => (
            <a key={n.id} href={`#/${n.id}`} className={page === n.id ? "active" : ""}>
              <span className="nav-icon">{n.icon}</span>{n.label}
            </a>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="health">
            <Dot ok={health.data ? health.data.status === "ready" : health.error ? false : null} pulse={!!health.data} />
            <span>{health.data ? `${health.data.status} · ${health.data.tools} tools` : health.error ? "gateway unreachable" : "connecting…"}</span>
          </div>
          {adminKey ? (
            <button className="btn btn-ghost btn-sm" onClick={() => setAdminKey(null)}>Sign out operator</button>
          ) : <span className="muted small">operator: signed out</span>}
        </div>
      </aside>

      <main className="main">
        {meta.data?.public_demo && (
          <div className="demo-banner">
            <b>Public sandbox.</b> Demo API keys are published on purpose, runtime changes expire, and all data resets every{" "}
            {Math.round(meta.data.demo.reset_interval_s / 60)} minutes. Please be kind to it.
          </div>
        )}
        {needsKey ? <SignIn onSignIn={setAdminKey} /> : (
          <>
            {page === "overview" && <Overview adminKey={adminKey!} traffic={traffic} onNavigate={navigate} />}
            {page === "agent" && <AgentPage info={meta.data?.agent ?? null} />}
            {page === "playground" && <Playground wire={wire} />}
            {page === "scenarios" && <Scenarios />}
            {page === "registry" && <Registry adminKey={adminKey!} backendHost={BACKEND_HOST} />}
            {page === "catalog" && <Catalog adminKey={adminKey!} />}
            {page === "policy" && <Policy adminKey={adminKey!} />}
            {page === "audit" && <Audit adminKey={adminKey!} />}
            {page === "experiment" && <Experiment />}
          </>
        )}
      </main>
    </div>
  );
}

function SignIn({ onSignIn }: { onSignIn: (k: string) => void }) {
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (k: string) => {
    setBusy(true); setError(null);
    try {
      const me = await http(k, "/v1/whoami");
      if (!me.is_admin) throw new Error(`'${me.id}' is not an operator (admin role required)`);
      onSignIn(k);
    } catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };

  return (
    <div className="signin">
      <div className="card signin-card">
        <h2>Operator sign-in</h2>
        <p className="muted">This view uses the admin API. Enter an API key with the <code>admin</code> role. It is kept in this tab's session storage only.</p>
        <form onSubmit={(e) => { e.preventDefault(); submit(key); }}>
          <input type="password" placeholder="mgw_…" value={key} onChange={(e) => setKey(e.target.value)} autoFocus />
          <button className="btn btn-primary" disabled={!key || busy}>Sign in</button>
        </form>
        <button className="btn btn-ghost" onClick={() => submit(DEMO_IDENTITIES.find((i) => i.role === "admin")!.key)}>Use demo operator key</button>
        {error && <div className="error-note">⚠ {error}</div>}
        <p className="muted small">The Agent Playground, Scenarios and Experiment pages don't need operator access.</p>
      </div>
    </div>
  );
}
