// Thin client for the gateway. Everything goes to the same origin (Vite proxy in dev, nginx in Docker).
// Every MCP JSON-RPC exchange is published to the wire log so the UI can show real traffic.

export interface Identity {
  id: string;
  label: string;
  key: string;
  role: string;
  blurb: string;
}

// Demo principals from examples/gateway.yaml (plaintext demo keys — never do this with real keys).
export const DEMO_IDENTITIES: Identity[] = [
  { id: "rider-bot", label: "Rider assistant", key: "demo-rider-key", role: "rider-agent",
    blurb: "Books rides & food, pays, opens tickets. Cannot refund. Charges limited to 5/min." },
  { id: "care-copilot", label: "Care copilot", key: "demo-support-key", role: "support-agent",
    blurb: "Support tickets, payments incl. refunds, ride & order lookups." },
  { id: "bi-analyst", label: "BI analyst", key: "demo-analyst-key", role: "analyst",
    blurb: "Every tool annotated read-only. No writes." },
  { id: "ops-admin", label: "Platform operator", key: "demo-admin-key", role: "admin",
    blurb: "Everything, plus the admin API." },
];

export interface WireEntry {
  seq: number;
  ts: number;
  identity: string;
  endpoint: string;
  method: string;
  request: unknown;
  response: unknown;
  status: number;
  ms: number;
  bytes: number;
  requestId: string | null;
  ok: boolean;
}

type Listener = (e: WireEntry) => void;
const listeners = new Set<Listener>();
let seq = 0;

export function onWire(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export class ApiError extends Error {
  constructor(public status: number, public body: any, message: string) {
    super(message);
  }
}

function headers(key: string | null, extra: Record<string, string> = {}): HeadersInit {
  return { ...(key ? { Authorization: `Bearer ${key}` } : {}), ...extra };
}

export async function http<T = any>(key: string | null, path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: headers(key, init.body ? { "content-type": "application/json" } : {}),
  });
  const text = await res.text();
  let body: any = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!res.ok) {
    const msg = (body && (body.message || body.detail)) || `HTTP ${res.status}`;
    throw new ApiError(res.status, body, typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return body as T;
}

export interface McpResponse {
  result?: any;
  error?: { code: number; message: string };
  status: number;
  ms: number;
  bytes: number;
  requestId: string | null;
}

let rpcId = 1;

/** One JSON-RPC request to a stateless MCP endpoint (`/mcp` or `/mcp/full`). */
export async function mcp(
  key: string, endpoint: "/mcp" | "/mcp/full", method: string, params: unknown, identity: string,
): Promise<McpResponse> {
  const request = { jsonrpc: "2.0", id: rpcId++, method, params };
  const started = performance.now();
  const res = await fetch(endpoint, {
    method: "POST",
    headers: headers(key, {
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
      "mcp-protocol-version": "2025-06-18",
    }),
    body: JSON.stringify(request),
  });
  const text = await res.text();
  const ms = Math.round(performance.now() - started);
  let body: any = null;
  try { body = JSON.parse(text); } catch { body = { error: { code: res.status, message: text || res.statusText } }; }
  const out: McpResponse = {
    result: body?.result, error: body?.error ?? (res.ok ? undefined : { code: res.status, message: body?.message ?? text }),
    status: res.status, ms, bytes: new Blob([text]).size, requestId: res.headers.get("x-request-id"),
  };
  const entry: WireEntry = {
    seq: ++seq, ts: Date.now(), identity, endpoint, method: method === "tools/call" ? `tools/call · ${(params as any)?.name}` : method,
    request, response: body, status: res.status, ms, bytes: out.bytes, requestId: out.requestId,
    ok: res.ok && !out.error && !out.result?.isError,
  };
  listeners.forEach((l) => l(entry));
  return out;
}

/** Parse the JSON text content of a CallToolResult (structuredContent if present). */
export function toolPayload(result: any): any {
  if (!result) return null;
  if (result.structuredContent) return result.structuredContent;
  const text = (result.content ?? []).map((c: any) => c.text ?? "").join("");
  try { return JSON.parse(text); } catch { return text; }
}

export function toolText(result: any): string {
  return (result?.content ?? []).map((c: any) => c.text ?? "").join("");
}

/** Rough token estimate (≈4 chars/token for JSON-heavy English); labelled as approximate in the UI. */
export const approxTokens = (bytes: number) => Math.round(bytes / 4);

export const fmt = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString());
