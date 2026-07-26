"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useAuth, API } from "../../lib/useAuth";
import { Nav } from "../Nav";
import { AnalyticsTabs } from "../AnalyticsTabs";
import { CacheNote } from "../CacheNote";

interface Row {
  load: string; customer: string; invoice_date: string;
  due_date: string; days_overdue: number; amount: number; currency: string;
}
interface Overdue {
  rows: Row[]; count: number;
  totals: Record<string, number>;
  aging_buckets: Record<string, Record<string, number>>;
  top_customers: { customer: string; currency: string; amount: number }[];
  as_of: string;
  _cache?: { hit: boolean; age_s: number };
}

const fmtFull = (n: number) => `$${Math.round(n).toLocaleString()}`;
const BUCKET_ORDER = ["1-30", "31-60", "61-90", "91-90+"];
const BUCKET_LABEL: Record<string, string> = { "1-30": "1–30d", "31-60": "31–60d", "61-90": "61–90d", "91-90+": "90d+" };

function Card({ label, value, sub, color }: { label: string; value: string; sub?: string; color: string }) {
  return (
    <div className="bg-[#1a1a1a] border border-[#2a2a2a] rounded-xl p-5">
      <div className="text-xs text-gray-500 uppercase tracking-wider">{label}</div>
      <div className={`text-2xl font-bold mt-1 ${color}`}>{value}</div>
      {sub && <div className="text-xs text-gray-500 mt-1">{sub}</div>}
    </div>
  );
}

function agingColor(days: number) {
  if (days > 90) return "text-red-400";
  if (days > 60) return "text-orange-400";
  if (days > 30) return "text-amber-300";
  return "text-yellow-500/80";
}

export default function OverduePage() {
  const { authReady, authorized, email, authHeaders, signIn, signOut, signingIn } = useAuth();
  const [data, setData] = useState<Overdue | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [cur, setCur] = useState<"ALL" | "CAD" | "USD">("ALL");

  const load = useCallback(async (force = false) => {
    setLoading(true); setErr(null);
    try {
      const url = `${API}/api/analytics/overdue${force ? "?refresh=true" : ""}`;
      const res = await fetch(url, { headers: authHeaders() });
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
      setData(await res.json());
    } catch (e) { setErr(String(e)); }
    setLoading(false);
  }, [authHeaders]);

  useEffect(() => { if (authorized) load(); }, [authorized, load]);

  const rows = useMemo(
    () => (data?.rows ?? []).filter((r) => cur === "ALL" || r.currency === cur),
    [data, cur],
  );

  if (!authReady) return <Shell><p className="text-gray-500">Loading…</p></Shell>;
  if (!authorized)
    return (
      <Shell>
        <div className="text-center py-20">
          <p className="text-gray-400 mb-4">{email ? `${email} is not authorized.` : "Sign in to view overdue AR."}</p>
          <button onClick={signIn} disabled={signingIn}
            className="px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-medium">
            {signingIn ? "Redirecting…" : "Sign in with Google"}
          </button>
        </div>
      </Shell>
    );

  const totals = data?.totals ?? {};
  const maxBucket = Math.max(1, ...Object.values(data?.aging_buckets ?? {}).flatMap((b) => Object.values(b)));

  return (
    <Shell email={email} onSignOut={signOut}>
      <AnalyticsTabs />
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">Overdue receivables</h1>
          <p className="text-sm text-gray-500">Invoices past due with no payment recorded · live from the Accounts sheet</p>
        </div>
        <div className="flex items-center gap-3">
          <CacheNote cache={data?._cache} />
          <button onClick={() => load(true)} className="text-xs text-gray-500 hover:text-gray-300">↻ refresh</button>
        </div>
      </div>

      {err && <p className="text-red-400 text-sm mb-4">Error: {err}</p>}
      {loading && !data && <p className="text-gray-500">Loading receivables…</p>}

      {data && (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
            <Card label="Owed (USD)" value={fmtFull(totals.USD ?? 0)} color="text-red-400" sub="past due" />
            <Card label="Owed (CAD)" value={fmtFull(totals.CAD ?? 0)} color="text-red-400" sub="past due" />
            <Card label="Invoices overdue" value={String(data.count)} color="text-amber-300" />
            <Card label="Worst" value={`${data.rows[0]?.days_overdue ?? 0}d`} color="text-orange-400"
              sub={data.rows[0]?.customer?.slice(0, 22)} />
          </div>

          <div className="grid lg:grid-cols-2 gap-6 mb-8">
            {/* Aging buckets */}
            <div className="bg-[#141414] border border-[#242424] rounded-xl p-5">
              <div className="text-sm font-semibold mb-4 text-gray-300">Aging</div>
              <div className="space-y-3">
                {BUCKET_ORDER.map((b) => {
                  const usd = data.aging_buckets[b]?.USD ?? 0;
                  const cad = data.aging_buckets[b]?.CAD ?? 0;
                  return (
                    <div key={b}>
                      <div className="flex justify-between text-xs mb-1">
                        <span className="text-gray-400">{BUCKET_LABEL[b]}</span>
                        <span className="text-gray-500">{fmtFull(usd)} USD · {fmtFull(cad)} CAD</span>
                      </div>
                      <div className="flex gap-1 h-2.5">
                        <div className="bg-rose-500/80 rounded-sm" style={{ width: `${(usd / maxBucket) * 100}%` }} />
                        <div className="bg-amber-500/70 rounded-sm" style={{ width: `${(cad / maxBucket) * 100}%` }} />
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Top debtors */}
            <div className="bg-[#141414] border border-[#242424] rounded-xl p-5">
              <div className="text-sm font-semibold mb-4 text-gray-300">Top debtors</div>
              <div className="space-y-2 max-h-56 overflow-y-auto">
                {data.top_customers.map((c, i) => (
                  <div key={i} className="flex justify-between text-sm">
                    <span className="text-gray-300 truncate max-w-[220px]">{c.customer}</span>
                    <span className="text-red-300 whitespace-nowrap">{fmtFull(c.amount)} <span className="text-gray-600 text-xs">{c.currency}</span></span>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Filter + table */}
          <div className="flex items-center gap-2 mb-3">
            {(["ALL", "USD", "CAD"] as const).map((c) => (
              <button key={c} onClick={() => setCur(c)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium ${cur === c ? "bg-[#262626] text-white" : "text-gray-500 hover:text-gray-300"}`}>
                {c}
              </button>
            ))}
            <span className="text-xs text-gray-600 ml-2">{rows.length} shown</span>
          </div>
          <div className="rounded-xl border border-[#222] overflow-x-auto">
            <table className="w-full text-sm min-w-[560px]">
              <thead className="bg-[#151515] text-gray-500 text-xs sticky top-0">
                <tr>
                  <th className="text-left px-4 py-2 font-medium">Load</th>
                  <th className="text-left px-4 py-2 font-medium">Customer</th>
                  <th className="text-left px-4 py-2 font-medium">Invoiced</th>
                  <th className="text-left px-4 py-2 font-medium">Due</th>
                  <th className="text-right px-4 py-2 font-medium">Overdue</th>
                  <th className="text-right px-4 py-2 font-medium">Amount</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.load} className="border-t border-[#1c1c1c] hover:bg-[#161616]">
                    <td className="px-4 py-2 text-gray-400">{r.load}</td>
                    <td className="px-4 py-2 text-gray-200 truncate max-w-[240px]">{r.customer}</td>
                    <td className="px-4 py-2 text-gray-500">{r.invoice_date}</td>
                    <td className="px-4 py-2 text-gray-500">{r.due_date}</td>
                    <td className={`px-4 py-2 text-right font-medium ${agingColor(r.days_overdue)}`}>{r.days_overdue}d</td>
                    <td className="px-4 py-2 text-right text-gray-200">{fmtFull(r.amount)} <span className="text-gray-600 text-xs">{r.currency}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-gray-600 mt-3">As of {data.as_of}.</p>
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
