"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useAuth, API } from "../../../lib/useAuth";
import { Nav } from "../../Nav";
import { AnalyticsTabs } from "../../AnalyticsTabs";
import { CacheNote } from "../../CacheNote";

interface CarrierRow {
  carrier: string; loads: number; spend: number; avg_load: number;
  revenue: number; margin: number; margin_pct: number; issues: number; issue_pct: number; currency: string;
}
interface Data { carriers: CarrierRow[]; count: number; as_of: string; _cache?: { hit: boolean; age_s: number } }

const fmt = (n: number) => `$${Math.round(n || 0).toLocaleString()}`;
type SortKey = "spend" | "loads" | "margin_pct" | "issues";

function Card({ label, value, sub, color }: { label: string; value: string; sub?: string; color: string }) {
  return (
    <div className="bg-[#1a1a1a] border border-[#2a2a2a] rounded-xl p-5">
      <div className="text-xs text-gray-500 uppercase tracking-wider">{label}</div>
      <div className={`text-2xl font-bold mt-1 ${color}`}>{value}</div>
      {sub && <div className="text-xs text-gray-500 mt-1 truncate">{sub}</div>}
    </div>
  );
}
function marginColor(p: number) { return p >= 18 ? "text-green-400" : p >= 10 ? "text-amber-300" : p >= 0 ? "text-orange-400" : "text-red-400"; }

function Shell({ children, email, onSignOut }: { children: React.ReactNode; email?: string; onSignOut?: () => void }) {
  return (
    <div className="min-h-screen bg-[#0d0d0d] text-white">
      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-6 sm:py-8">
        <Nav email={email} onSignOut={onSignOut} />
        {children}
      </div>
    </div>
  );
}

export default function CarriersPage() {
  const { authReady, authorized, email, authHeaders, signIn, signOut, signingIn } = useAuth();
  const [data, setData] = useState<Data | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<SortKey>("spend");

  const load = useCallback(async (force = false) => {
    setLoading(true); setErr(null);
    try {
      const res = await fetch(`${API}/api/analytics/carriers${force ? "?refresh=true" : ""}`, { headers: authHeaders() });
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
      setData(await res.json());
    } catch (e) { setErr(String(e)); }
    setLoading(false);
  }, [authHeaders]);
  useEffect(() => { if (authorized) load(); }, [authorized, load]);

  const rows = useMemo(() => {
    let r = data?.carriers ?? [];
    if (q.trim()) r = r.filter((c) => c.carrier.toLowerCase().includes(q.trim().toLowerCase()));
    return [...r].sort((a, b) => (b[sort] as number) - (a[sort] as number));
  }, [data, q, sort]);

  if (!authReady) return <Shell><p className="text-gray-500">Loading…</p></Shell>;
  if (!authorized)
    return (
      <Shell>
        <div className="text-center py-20">
          <p className="text-gray-400 mb-4">{email ? `${email} is not authorized.` : "Sign in to view carriers."}</p>
          <button onClick={signIn} disabled={signingIn} className="px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-medium">
            {signingIn ? "Redirecting…" : "Sign in with Google"}
          </button>
        </div>
      </Shell>
    );

  const meaningful = (data?.carriers ?? []).filter((c) => c.loads >= 3);
  const biggest = data?.carriers?.[0];
  const mostIssues = [...(data?.carriers ?? [])].sort((a, b) => b.issues - a.issues)[0];
  const bestMargin = [...meaningful].sort((a, b) => b.margin_pct - a.margin_pct)[0];

  return (
    <Shell email={email} onSignOut={signOut}>
      <AnalyticsTabs />
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">Carriers</h1>
          <p className="text-sm text-gray-500">Volume, spend, the margin we earn, and issue rate per carrier</p>
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
            <Card label="Biggest by spend" value={biggest ? fmt(biggest.spend) : "—"} color="text-orange-400" sub={biggest?.carrier} />
            <Card label="Best margin (3+ loads)" value={bestMargin ? `${bestMargin.margin_pct}%` : "—"} color="text-green-400" sub={bestMargin?.carrier} />
            <Card label="Most flagged" value={mostIssues?.issues ? String(mostIssues.issues) : "0"}
              color={mostIssues?.issues ? "text-red-400" : "text-gray-400"} sub={mostIssues?.issues ? mostIssues.carrier : "no payment issues"} />
          </div>

          <div className="flex flex-wrap items-center gap-2 mb-3">
            <span className="text-xs text-gray-600">sort</span>
            {([["spend", "Spend"], ["loads", "Loads"], ["margin_pct", "Margin %"], ["issues", "Issues"]] as [SortKey, string][]).map(([k, label]) => (
              <button key={k} onClick={() => setSort(k)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium ${sort === k ? "bg-[#262626] text-white" : "text-gray-500 hover:text-gray-300"}`}>
                {label}
              </button>
            ))}
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search carrier…"
              className="ml-auto bg-[#141414] border border-[#242424] rounded-lg px-3 py-1.5 text-xs text-gray-200 placeholder-gray-600 focus:outline-none focus:border-[#333] w-48" />
          </div>

          <div className="rounded-xl border border-[#222] overflow-x-auto">
            <table className="w-full text-sm min-w-[640px]">
              <thead className="bg-[#151515] text-gray-500 text-xs">
                <tr>
                  <th className="text-left px-4 py-2 font-medium">Carrier</th>
                  <th className="text-right px-4 py-2 font-medium">Loads</th>
                  <th className="text-right px-4 py-2 font-medium">Spend</th>
                  <th className="text-right px-4 py-2 font-medium">Avg/load</th>
                  <th className="text-right px-4 py-2 font-medium">Our margin %</th>
                  <th className="text-right px-4 py-2 font-medium">Issues</th>
                </tr>
              </thead>
              <tbody>
                {rows.slice(0, 200).map((c, i) => (
                  <tr key={`${c.carrier}-${i}`} className="border-t border-[#1c1c1c] hover:bg-[#161616]">
                    <td className="px-4 py-2 text-gray-200 truncate max-w-[260px]">{c.carrier}
                      <span className="text-gray-600 text-xs ml-1.5">{c.currency}</span></td>
                    <td className="px-4 py-2 text-right text-gray-400">{c.loads}</td>
                    <td className="px-4 py-2 text-right text-gray-200">{fmt(c.spend)}</td>
                    <td className="px-4 py-2 text-right text-gray-400">{fmt(c.avg_load)}</td>
                    <td className={`px-4 py-2 text-right font-medium ${marginColor(c.margin_pct)}`}>{c.margin_pct}%</td>
                    <td className={`px-4 py-2 text-right ${c.issues ? "text-red-400" : "text-gray-600"}`}>{c.issues || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-gray-600 mt-3">{rows.length} carriers · showing up to 200 · as of {data.as_of}. Margin % uses loads where both shipper and carrier amounts are set.</p>
        </>
      )}
    </Shell>
  );
}
