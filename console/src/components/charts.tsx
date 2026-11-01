// Small dependency-free SVG charts.

export function Sparkbars({ series, height = 90, colors }: {
  series: { label: string; values: number[] }[]; height?: number; colors: string[];
}) {
  const n = Math.max(0, ...series.map((s) => s.values.length));
  if (!n) return <div className="muted small">Collecting samples…</div>;
  const totals = Array.from({ length: n }, (_, i) => series.reduce((a, s) => a + (s.values[i] ?? 0), 0));
  const max = Math.max(1, ...totals);
  const w = 100 / Math.max(n, 40);
  return (
    <svg className="sparkbars" viewBox={`0 0 100 ${height}`} preserveAspectRatio="none" style={{ height }}>
      {Array.from({ length: n }, (_, i) => {
        let y = height;
        const x = 100 - (n - i) * w;
        return series.map((s, si) => {
          const v = s.values[i] ?? 0;
          const h = (v / max) * (height - 4);
          y -= h;
          return v ? <rect key={`${i}-${si}`} x={x + w * 0.12} y={y} width={w * 0.76} height={h} fill={colors[si]} rx={0.4} /> : null;
        });
      })}
    </svg>
  );
}

export function HBarCompare({ rows, aLabel, bLabel, format = (n: number) => n.toLocaleString() }: {
  rows: { label: string; a: number; b: number; note?: string }[]; aLabel: string; bLabel: string; format?: (n: number) => string;
}) {
  const max = Math.max(1, ...rows.flatMap((r) => [r.a, r.b]));
  return (
    <div className="hbar">
      <div className="legend">
        <span><i className="sw sw-a" />{aLabel}</span>
        <span><i className="sw sw-b" />{bLabel}</span>
      </div>
      {rows.map((r) => {
        const delta = (100 * (r.b - r.a)) / r.a;
        return (
          <div className="hbar-row" key={r.label}>
            <div className="hbar-label">{r.label}{r.note && <span className="muted small"> · {r.note}</span>}</div>
            <div className="hbar-bars">
              <div className="hbar-track"><div className="hbar-a" style={{ width: `${(100 * r.a) / max}%` }} /><span>{format(r.a)}</span></div>
              <div className="hbar-track"><div className="hbar-b" style={{ width: `${(100 * r.b) / max}%` }} /><span>{format(r.b)}</span></div>
            </div>
            <div className={`hbar-delta ${delta <= 0 ? "good" : "bad"}`}>{delta > 0 ? "+" : ""}{delta.toFixed(0)}%</div>
          </div>
        );
      })}
    </div>
  );
}

/** Log-scale bars for values spanning orders of magnitude. */
export function LogBars({ rows }: { rows: { label: string; value: number; tone: "a" | "b" }[] }) {
  const max = Math.log10(Math.max(10, ...rows.map((r) => r.value)));
  return (
    <div className="logbars">
      {rows.map((r, i) => (
        <div className="logbar-row" key={i}>
          <div className="logbar-label">{r.label}</div>
          <div className="hbar-track"><div className={`hbar-${r.tone}`} style={{ width: `${(100 * Math.log10(Math.max(1, r.value))) / max}%` }} />
            <span>{r.value.toLocaleString()}</span></div>
        </div>
      ))}
    </div>
  );
}
