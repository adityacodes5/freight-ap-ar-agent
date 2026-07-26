"use client";

import { useCallback, useEffect, useState } from "react";
import { useAuth, API } from "../../lib/useAuth";
import { Nav } from "../Nav";
import { AnalyticsTabs } from "../AnalyticsTabs";
import { CacheNote } from "../CacheNote";

interface Month {
  month: string; label: string;
  inflow_cad: number; inflow_usd: number;
  outflow_cad: number; outflow_usd: number;
  net_cad: number; net_usd: number;
}
interface Flows { inflow_cad: number; inflow_usd: number; outflow_cad: number; outflow_usd: number }
interface Cashflow {
  months: Month[];
  totals: Flows;
  outstanding: { ar_cad: number; ar_usd: number; ap_cad: number; ap_usd: number };
  expected: Flows;
  as_of: string;
  _cache?: { hit: boolean; age_s: number };
}

const fmt = (n: number) => (Math.abs(n) >= 1000 ? `$${(n / 1000).toFixed(0)}k` : `$${n.toFixed(0)}`);
const fmtFull = (n: number) => `$${Math.round(n).toLocaleString()}`;

function Card({ label, value, sub, color }: { label: string; value: string; sub?: string; color: string }) {
  return (
    <div className="bg-[#1a1a1a] border border-[#2a2a2a] rounded-xl p-5">
      <div className="text-xs text-gray-500 uppercase tracking-wider">{label}</div>
      <div className={`text-2xl font-bold mt-1 ${color}`}>{value}</div>
      {sub && <div className="text-xs text-gray-500 mt-1">{sub}</div>}
    </div>
  );
}

/** Grouped inflow-vs-outflow bar chart for one currency, hand-drawn SVG. */
function CashChart({ months, cur }: { months: Month[]; cur: "cad" | "usd" }) {
  const W = 860, H = 300, padL = 56, padR = 16, padT = 16, padB = 44;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const inflow = (m: Month) => (cur === "cad" ? m.inflow_cad : m.inflow_usd);
  const outflow = (m: Month) => (cur === "cad" ? m.outflow_cad : m.outflow_usd);
  const max = Math.max(1, ...months.flatMap((m) => [inflow(m), outflow(m)]));
  const groupW = plotW / Math.max(months.length, 1);
  const barW = Math.min(18, groupW * 0.32);
  const y = (v: number) => padT + plotH - (v / max) * plotH;
  const gridVals = [0, 0.25, 0.5, 0.75, 1].map((f) => f * max);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ maxHeight: 320 }}>
      {gridVals.map((v, i) => (
        <g key={i}>
          <line x1={padL} x2={W - padR} y1={y(v)} y2={y(v)} stroke="#242424" strokeWidth={1} />
          <text x={padL - 8} y={y(v) + 4} textAnchor="end" fontSize={10} fill="#666">{fmt(v)}</text>
        </g>
      ))}
      {months.map((m, i) => {
        const gx = padL + i * groupW + groupW / 2;
        const inV = inflow(m), outV = outflow(m);
        return (
          <g key={m.month}>
            <rect x={gx - barW - 2} y={y(inV)} width={barW} height={padT + plotH - y(inV)} rx={2} fill="#22c55e">
              <title>{`${m.label} — in ${fmtFull(inV)}`}</title>
            </rect>
            <rect x={gx + 2} y={y(outV)} width={barW} height={padT + plotH - y(outV)} rx={2} fill="#f43f5e">
              <title>{`${m.label} — out ${fmtFull(outV)}`}</title>
            </rect>
            <text x={gx} y={H - padB + 16} textAnchor="middle" fontSize={9} fill="#777"
              transform={months.length > 12 ? `rotate(35 ${gx} ${H - padB + 16})` : undefined}>
              {m.label.replace(" 20", " '")}
            </text>
          </g>
        );
      })}
      <line x1={padL} x2={W - padR} y1={padT + plotH} y2={padT + plotH} stroke="#333" strokeWidth={1} />
    </svg>
  );
}

export default function AnalyticsPage() {
  const { authReady, authorized, email, authHeaders, signIn, signOut, signingIn } = useAuth();
  const [data, setData] = useState<Cashflow | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (force = false) => {
    setLoading(true); setErr(null);
    try {
      const url = `${API}/api/analytics/cashflow${force ? "?refresh=true" : ""}`;
      const res = await fetch(url, { headers: authHeaders() });
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
      setData(await res.json());
    } catch (e) { setErr(String(e)); }
    setLoading(false);
  }, [authHeaders]);

  useEffect(() => { if (authorized) load(); }, [authorized, load]);

  if (!authReady) return <Shell><p className="text-gray-500">Loading…</p></Shell>;
  if (!authorized)
    return (
      <Shell>
        <div className="text-center py-20">
          <p className="text-gray-400 mb-4">{email ? `${email} is not authorized.` : "Sign in to view analytics."}</p>
          <button onClick={signIn} disabled={signingIn}
            className="px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-medium">
            {signingIn ? "Redirecting…" : "Sign in with Google"}
          </button>
        </div>
      </Shell>
    );

  const t = data?.totals;
  return (
    <Shell email={email} onSignOut={signOut}>
      <AnalyticsTabs />
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">Cash flow analytics</h1>
          <p className="text-sm text-gray-500">Inflow = payments received · Outflow = carrier payments · CAD and USD kept separate</p>
        </div>
        <div className="flex items-center gap-3">
          <CacheNote cache={data?._cache} />
          <button onClick={() => load(true)} className="text-xs text-gray-500 hover:text-gray-300">↻ refresh</button>
        </div>
      </div>

      {err && <p className="text-red-400 text-sm mb-4">Error: {err}</p>}
      {loading && !data && <p className="text-gray-500">Crunching the numbers…</p>}

      {t && (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
            <Card label="Net CAD" value={fmtFull(t.inflow_cad - t.outflow_cad)} color={t.inflow_cad - t.outflow_cad >= 0 ? "text-green-400" : "text-red-400"}
              sub={`${fmtFull(t.inflow_cad)} in · ${fmtFull(t.outflow_cad)} out`} />
            <Card label="Net USD" value={fmtFull(t.inflow_usd - t.outflow_usd)} color={t.inflow_usd - t.outflow_usd >= 0 ? "text-green-400" : "text-red-400"}
              sub={`${fmtFull(t.inflow_usd)} in · ${fmtFull(t.outflow_usd)} out`} />
            <Card label="Total inflow" value={`${fmtFull(t.inflow_cad)} / ${fmtFull(t.inflow_usd)}`} color="text-green-400" sub="CAD / USD received" />
            <Card label="Total outflow" value={`${fmtFull(t.outflow_cad)} / ${fmtFull(t.outflow_usd)}`} color="text-rose-400" sub="CAD / USD paid to carriers" />
          </div>

          {/* If everything due today were settled */}
          <div className="bg-[#141414] border border-[#242424] rounded-xl p-5 mb-8">
            <div className="text-sm font-semibold text-gray-300">If everything due today were settled</div>
            <p className="text-xs text-gray-500 mb-4">Actual flows plus what&apos;s still outstanding and past due — every overdue invoice collected, every overdue carrier paid.</p>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
              <Card label="Exp. inflow (CAD)" value={fmtFull(data?.expected?.inflow_cad ?? 0)} color="text-green-400" sub={`+${fmtFull(data?.outstanding?.ar_cad ?? 0)} to collect`} />
              <Card label="Exp. inflow (USD)" value={fmtFull(data?.expected?.inflow_usd ?? 0)} color="text-green-400" sub={`+${fmtFull(data?.outstanding?.ar_usd ?? 0)} to collect`} />
              <Card label="Exp. outflow (CAD)" value={fmtFull(data?.expected?.outflow_cad ?? 0)} color="text-rose-400" sub={`+${fmtFull(data?.outstanding?.ap_cad ?? 0)} to pay`} />
              <Card label="Exp. outflow (USD)" value={fmtFull(data?.expected?.outflow_usd ?? 0)} color="text-rose-400" sub={`+${fmtFull(data?.outstanding?.ap_usd ?? 0)} to pay`} />
            </div>
          </div>

          <Legend />
          {(["cad", "usd"] as const).map((cur) => (
            <div key={cur} className="bg-[#141414] border border-[#242424] rounded-xl p-5 mb-6">
              <div className="text-sm font-semibold mb-3 text-gray-300">{cur.toUpperCase()} — monthly inflow vs outflow</div>
              {data && <CashChart months={data.months} cur={cur} />}
            </div>
          ))}
          <p className="text-xs text-gray-600">As of {data?.as_of}. Last {data?.months.length} months.</p>
        </>
      )}
    </Shell>
  );
}

function Legend() {
  return (
    <div className="flex items-center gap-5 text-xs text-gray-400 mb-3">
      <span className="flex items-center gap-1.5"><span className="w-3 h-3 rounded-sm bg-[#22c55e] inline-block" /> Inflow (received)</span>
      <span className="flex items-center gap-1.5"><span className="w-3 h-3 rounded-sm bg-[#f43f5e] inline-block" /> Outflow (paid)</span>
    </div>
  );
}

function Shell({ children, email, onSignOut }: { children: React.ReactNode; email?: string; onSignOut?: () => void }) {
  return (
    <div className="min-h-screen bg-[#0d0d0d] text-white">
      <div className="max-w-6xl mx-auto px-6 py-8">
        <Nav email={email} onSignOut={onSignOut} />
        {children}
      </div>
    </div>
  );
}
