"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useAuth, API } from "../../../lib/useAuth";
import { Nav } from "../../Nav";
import { AnalyticsTabs } from "../../AnalyticsTabs";
import { CacheNote } from "../../CacheNote";

interface CustomerRow {
  customer: string; currency: string; loads: number;
  revenue: number; margin: number; margin_pct: number;
  avg_days_to_pay: number | null; on_time_pct: number | null; paid_count: number;
}
interface Performance {
  customers: CustomerRow[];
  as_of: string;
  _cache?: { hit: boolean; age_s: number };
}

const fmtFull = (n: number) => `$${Math.round(n).toLocaleString()}`;
type SortKey = "revenue" | "margin_pct" | "avg_days_to_pay" | "loads";

function Card({ label, value, sub, color }: { label: string; value: string; sub?: string; color: string }) {
  return (
    <div className="bg-[#1a1a1a] border border-[#2a2a2a] rounded-xl p-5">
      <div className="text-xs text-gray-500 uppercase tracking-wider">{label}</div>
      <div className={`text-2xl font-bold mt-1 ${color}`}>{value}</div>
      {sub && <div className="text-xs text-gray-500 mt-1 truncate">{sub}</div>}
    </div>
  );
}
function marginColor(pct: number) {
  if (pct >= 18) return "text-green-400";
  if (pct >= 10) return "text-amber-300";
  if (pct >= 0) return "text-orange-400";
  return "text-red-400";
}
function payColor(days: number | null) {
  if (days === null) return "text-gray-600";
  if (days <= 30) return "text-green-400";
  if (days <= 45) return "text-amber-300";
  return "text-red-400";
}

export default function CustomersPage() {
  const { authReady, authorized, email, authHeaders, signIn, signOut, signingIn } = useAuth();
  const [data, setData] = useState<Performance | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [cur, setCur] = useState<"ALL" | "CAD" | "USD">("ALL");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<SortKey>("revenue");

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

  const rows = useMemo(() => {
    let r = (data?.customers ?? []).filter((c) => cur === "ALL" || c.currency === cur);
    if (q.trim()) r = r.filter((c) => c.customer.toLowerCase().includes(q.trim().toLowerCase()));
    const sorted = [...r].sort((a, b) => {
      if (sort === "avg_days_to_pay") {
        const av = a.avg_days_to_pay ?? Infinity, bv = b.avg_days_to_pay ?? Infinity;
        return av - bv; // fastest payers first
      }
      return (b[sort] as number) - (a[sort] as number);
    });
    return sorted;
  }, [data, cur, q, sort]);

  if (!authReady) return <Shell><p className="text-gray-500">Loading…</p></Shell>;
  if (!authorized)
    return (
      <Shell>
        <div className="text-center py-20">
          <p className="text-gray-400 mb-4">{email ? `${email} is not authorized.` : "Sign in to view customers."}</p>
          <button onClick={signIn} disabled={signingIn}
            className="px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-medium">
            {signingIn ? "Redirecting…" : "Sign in with Google"}
          </button>
        </div>
      </Shell>
    );

  // headline picks (min 3 loads so a one-off doesn't top the chart)
  const meaningful = (data?.customers ?? []).filter((c) => c.loads >= 3);
  const bestMargin = [...meaningful].sort((a, b) => b.margin_pct - a.margin_pct)[0];
  const slowest = [...meaningful].filter((c) => c.avg_days_to_pay !== null)
    .sort((a, b) => (b.avg_days_to_pay ?? 0) - (a.avg_days_to_pay ?? 0))[0];
  const biggest = [...(data?.customers ?? [])].sort((a, b) => b.revenue - a.revenue)[0];

  return (
    <Shell email={email} onSignOut={signOut}>
      <AnalyticsTabs />
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">Customers</h1>
          <p className="text-sm text-gray-500">Margin and payment behaviour per customer · one row per customer + currency</p>
        </div>
        <div className="flex items-center gap-3">
          <CacheNote cache={data?._cache} />
          <button onClick={() => load(true)} className="text-xs text-gray-500 hover:text-gray-300">↻ refresh</button>
        </div>
      </div>

      {err && <p className="text-red-400 text-sm mb-4">Error: {err}</p>}
      {loading && !data && <p className="text-gray-500">Crunching the numbers…</p>}

      {data && (
        <>
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-8">
            <Card label="Biggest by revenue" value={biggest ? fmtFull(biggest.revenue) : "—"}
              color="text-sky-400" sub={biggest ? `${biggest.customer} · ${biggest.currency}` : ""} />
            <Card label="Best margin (3+ loads)" value={bestMargin ? `${bestMargin.margin_pct}%` : "—"}
              color="text-green-400" sub={bestMargin ? bestMargin.customer : ""} />
            <Card label="Slowest to pay (3+ loads)" value={slowest?.avg_days_to_pay != null ? `${slowest.avg_days_to_pay}d` : "—"}
              color="text-red-400" sub={slowest ? slowest.customer : ""} />
          </div>

          <div className="flex flex-wrap items-center gap-2 mb-3">
            {(["ALL", "USD", "CAD"] as const).map((c) => (
              <button key={c} onClick={() => setCur(c)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium ${cur === c ? "bg-[#262626] text-white" : "text-gray-500 hover:text-gray-300"}`}>
                {c}
              </button>
            ))}
            <span className="text-gray-700">·</span>
            <span className="text-xs text-gray-600">sort</span>
            {([["revenue", "Revenue"], ["margin_pct", "Margin %"], ["avg_days_to_pay", "Fastest pay"], ["loads", "Loads"]] as [SortKey, string][]).map(([k, label]) => (
              <button key={k} onClick={() => setSort(k)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium ${sort === k ? "bg-[#262626] text-white" : "text-gray-500 hover:text-gray-300"}`}>
                {label}
              </button>
            ))}
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search customer…"
              className="ml-auto bg-[#141414] border border-[#242424] rounded-lg px-3 py-1.5 text-xs text-gray-200 placeholder-gray-600 focus:outline-none focus:border-[#333] w-48" />
          </div>

          <div className="rounded-xl border border-[#222] overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-[#151515] text-gray-500 text-xs">
                <tr>
                  <th className="text-left px-4 py-2 font-medium">Customer</th>
                  <th className="text-right px-4 py-2 font-medium">Loads</th>
                  <th className="text-right px-4 py-2 font-medium">Revenue</th>
                  <th className="text-right px-4 py-2 font-medium">Margin</th>
                  <th className="text-right px-4 py-2 font-medium">Margin %</th>
                  <th className="text-right px-4 py-2 font-medium">Avg pay</th>
                  <th className="text-right px-4 py-2 font-medium">On time</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((c, i) => (
                  <tr key={`${c.customer}-${c.currency}-${i}`} className="border-t border-[#1c1c1c] hover:bg-[#161616]">
                    <td className="px-4 py-2 text-gray-200 truncate max-w-[260px]">{c.customer}
                      <span className="text-gray-600 text-xs ml-1.5">{c.currency}</span></td>
                    <td className="px-4 py-2 text-right text-gray-400">{c.loads}</td>
                    <td className="px-4 py-2 text-right text-gray-200">{fmtFull(c.revenue)}</td>
                    <td className="px-4 py-2 text-right text-gray-300">{fmtFull(c.margin)}</td>
                    <td className={`px-4 py-2 text-right font-medium ${marginColor(c.margin_pct)}`}>{c.margin_pct}%</td>
                    <td className={`px-4 py-2 text-right ${payColor(c.avg_days_to_pay)}`}>
                      {c.avg_days_to_pay != null ? `${c.avg_days_to_pay}d` : "—"}</td>
                    <td className="px-4 py-2 text-right text-gray-400">
                      {c.on_time_pct != null ? `${c.on_time_pct}%` : "—"}</td>
                  </tr>
                ))}
                {rows.length === 0 && (
                  <tr><td colSpan={7} className="px-4 py-8 text-center text-gray-600">No customers match.</td></tr>
                )}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-gray-600 mt-3">{rows.length} shown · as of {data.as_of}. Margin % and avg-pay use only loads that are fully invoiced / paid.</p>
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
