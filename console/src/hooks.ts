import { useCallback, useEffect, useRef, useState } from "react";
import { onWire, type WireEntry } from "./api";

/** Poll an async function. Pauses while the tab is hidden. */
export function usePoll<T>(fn: () => Promise<T>, intervalMs: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const refresh = useCallback(async () => {
    try {
      const d = await fnRef.current();
      setData(d);
      setError(null);
    } catch (e: any) {
      setError(e?.message ?? String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    refresh();
    const t = setInterval(() => { if (alive && !document.hidden) refresh(); }, intervalMs);
    return () => { alive = false; clearInterval(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, refresh, ...deps]);

  return { data, error, loading, refresh };
}

/** Rolling buffer of MCP wire traffic. */
export function useWire(limit = 200) {
  const [entries, setEntries] = useState<WireEntry[]>([]);
  useEffect(() => onWire((e) => setEntries((prev) => [e, ...prev].slice(0, limit))), [limit]);
  return { entries, clear: () => setEntries([]) };
}

export function useSessionState<T>(key: string, initial: T) {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = sessionStorage.getItem(key);
      return raw ? (JSON.parse(raw) as T) : initial;
    } catch {
      return initial;
    }
  });
  useEffect(() => { sessionStorage.setItem(key, JSON.stringify(value)); }, [key, value]);
  return [value, setValue] as const;
}
