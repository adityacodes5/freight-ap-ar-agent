"use client";

import { useCallback, useEffect, useState } from "react";
import { useAuth, API } from "../../../lib/useAuth";
import { Nav } from "../../Nav";
import { AnalyticsTabs } from "../../AnalyticsTabs";
import { CacheNote } from "../../CacheNote";

interface MarginAgg {
  loads: number; revenue: number; cost: number; margin: number; avg_margin: number; margin_pct: number;
}
interface MarginMonth {
  month: string; label: string;
  margin_cad: number; margin_usd: number; revenue_cad: number; revenue_usd: number;
}
interface Performance {
  margin: { CAD: MarginAgg; USD: MarginAgg };
  margin_months: MarginMonth[];
  payment: {
    avg_days_to_pay: number; median_days: number; on_time_pct: number;
    paid_count: number; distribution: Record<string, number>;
  };
  customers: unknown[];
  as_of: string;
  _cache?: { hit: boolean; age_s: number };
}

const fmt = (n: number) => (Math.abs(n) >= 1000 ? `$${(n / 1000).toFixed(0)}k` : `$${n.toFixed(0)}`);
const fmtFull = (n: number) => `$${Math.round(n).toLocaleString()}`;
const SPEED_ORDER = ["0-15", "16-30", "31-45", "46-60", "60+"];
const SPEED_LABEL: Record<string, string> = {
  "0-15": "0–15 days", "16-30": "16–30", "31-45": "31–45", "46-60": "46–60", "60+": "60+ days",
};

function Card({ label, value, sub, color }: { label: string; value: string; sub?: string; color: string }) {
  return (
    <div className="bg-[#1a1a1a] border border-[#2a2a2a] rounded-xl p-5">
      <div className="text-xs text-gray-500 uppercase tracking-wider">{label}</div>
      <div className={`text-2xl font-bold mt-1 ${color}`}>{value}</div>
      {sub && <div className="text-xs text-gray-500 mt-1">{sub}</div>}
    </div>
  );
}

/** Grouped monthly margin bars (CAD vs USD), hand-drawn SVG. */
function MarginChart({ months }: { months: MarginMonth[] }) {
  const W = 860, H = 300, padL = 56, padR = 16, padT = 16, padB = 44;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const max = Math.max(1, ...months.flatMap((m) => [m.margin_cad, m.margin_usd]));
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
        return (
          <g key={m.month}>
            <rect x={gx - barW - 2} y={y(m.margin_cad)} width={barW} height={padT + plotH - y(m.margin_cad)} rx={2} fill="#a855f7">
              <title>{`${m.label} — CAD margin ${fmtFull(m.margin_cad)}`}</title>
            </rect>
            <rect x={gx + 2} y={y(m.margin_usd)} width={barW} height={padT + plotH - y(m.margin_usd)} rx={2} fill="#38bdf8">
              <title>{`${m.label} — USD margin ${fmtFull(m.margin_usd)}`}</title>
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

export default function PerformancePage() {
  const { authReady, authorized, email, authHeaders, signIn, signOut, signingIn } = useAuth();
  const [data, setData] = useState<Performance | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (force = false) => {
    setLoading(true); setErr(null);
    try {
      const url = `${API}/api/analytics/performance${force ? "?refresh=true" : ""}`;
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
          <p className="text-gray-400 mb-4">{email ? `${email} is not authorized.` : "Sign in to view performance."}</p>
          <button onClick={signIn} disabled={signingIn}
            className="px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-medium">
            {signingIn ? "Redirecting…" : "Sign in with Google"}
          </button>
        </div>
      </Shell>
    );

  const m = data?.margin;
  const pay = data?.payment;
  const maxSpeed = Math.max(1, ...Object.values(pay?.distribution ?? {}));

  return (
    <Shell email={email} onSignOut={signOut}>
      <AnalyticsTabs />
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">Margins &amp; payment timing</h1>
          <p className="text-sm text-gray-500">Revenue vs carrier cost, and how fast customers pay · pre-tax, both sides invoiced</p>
        </div>
        <div className="flex items-center gap-3">
          <CacheNote cache={data?._cache} />
          <button onClick={() => load(true)} className="text-xs text-gray-500 hover:text-gray-300">↻ refresh</button>
        </div>
      </div>

      {err && <p className="text-red-400 text-sm mb-4">Error: {err}</p>}
      {loading && !data && <p className="text-gray-500">Crunching the numbers…</p>}

      {data && m && pay && (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
            <Card label="CAD margin" value={`${m.CAD.margin_pct}%`} color="text-fuchsia-400"
              sub={`${fmtFull(m.CAD.margin)} on ${fmtFull(m.CAD.revenue)} · ${m.CAD.loads} loads`} />
            <Card label="USD margin" value={`${m.USD.margin_pct}%`} color="text-sky-400"
              sub={`${fmtFull(m.USD.margin)} on ${fmtFull(m.USD.revenue)} · ${m.USD.loads} loads`} />
            <Card label="Avg days to pay" value={`${pay.avg_days_to_pay}d`} color="text-amber-300"
              sub={`median ${pay.median_days}d · ${pay.paid_count} paid invoices`} />
            <Card label="Paid on time" value={`${pay.on_time_pct}%`}
              color={pay.on_time_pct >= 70 ? "text-green-400" : pay.on_time_pct >= 50 ? "text-amber-300" : "text-red-400"}
              sub="by the due date" />
          </div>

          {/* Margin detail */}
          <div className="grid lg:grid-cols-2 gap-4 mb-8">
            {(["CAD", "USD"] as const).map((c) => {
              const a = m[c];
              const accent = c === "CAD" ? "text-fuchsia-400" : "text-sky-400";
              return (
                <div key={c} className="bg-[#141414] border border-[#242424] rounded-xl p-5">
                  <div className="flex items-baseline justify-between mb-4">
                    <div className="text-sm font-semibold text-gray-300">{c} margin</div>
                    <div className={`text-lg font-bold ${accent}`}>{a.margin_pct}%</div>
                  </div>
                  <div className="grid grid-cols-3 gap-3 text-center">
                    <div><div className="text-xs text-gray-500">Revenue</div><div className="text-sm text-gray-200 mt-0.5">{fmtFull(a.revenue)}</div></div>
                    <div><div className="text-xs text-gray-500">Carrier cost</div><div className="text-sm text-gray-200 mt-0.5">{fmtFull(a.cost)}</div></div>
                    <div><div className="text-xs text-gray-500">Gross margin</div><div className={`text-sm mt-0.5 font-medium ${accent}`}>{fmtFull(a.margin)}</div></div>
                  </div>
                  <div className="text-xs text-gray-600 mt-4">Avg {fmtFull(a.avg_margin)} / load across {a.loads} loads</div>
                </div>
              );
            })}
          </div>

          {/* Monthly margin trend */}
          <div className="bg-[#141414] border border-[#242424] rounded-xl p-5 mb-8">
            <div className="flex items-center justify-between mb-3">
              <div className="text-sm font-semibold text-gray-300">Monthly gross margin</div>
              <div className="flex items-center gap-4 text-xs text-gray-400">
                <span className="flex items-center gap-1.5"><span className="w-3 h-3 rounded-sm bg-[#a855f7] inline-block" /> CAD</span>
                <span className="flex items-center gap-1.5"><span className="w-3 h-3 rounded-sm bg-[#38bdf8] inline-block" /> USD</span>
              </div>
            </div>
            {data.margin_months.length ? <MarginChart months={data.margin_months} />
              : <p className="text-gray-600 text-sm py-8 text-center">No dated margin data yet.</p>}
          </div>

          {/* Payment-speed distribution */}
          <div className="bg-[#141414] border border-[#242424] rounded-xl p-5">
            <div className="text-sm font-semibold mb-1 text-gray-300">How long customers take to pay</div>
            <p className="text-xs text-gray-500 mb-4">Days from invoice date to payment received · {pay.paid_count} paid invoices</p>
            <div className="space-y-3">
              {SPEED_ORDER.map((b) => {
                const n = pay.distribution[b] ?? 0;
                const pct = pay.paid_count ? Math.round((n / pay.paid_count) * 100) : 0;
                return (
                  <div key={b}>
                    <div className="flex justify-between text-xs mb-1">
                      <span className="text-gray-400">{SPEED_LABEL[b]}</span>
                      <span className="text-gray-500">{n} · {pct}%</span>
                    </div>
                    <div className="h-2.5 bg-[#1c1c1c] rounded-sm overflow-hidden">
                      <div className="h-full bg-amber-500/70 rounded-sm" style={{ width: `${(n / maxSpeed) * 100}%` }} />
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          <p className="text-xs text-gray-600 mt-4">As of {data.as_of}. Margin counts loads with both a shipper and a carrier amount; HST excluded.</p>
        </>
      )}
    </Shell>
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
