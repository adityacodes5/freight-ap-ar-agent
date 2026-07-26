"use client";

import { useCallback, useState } from "react";
import { useAuth, API } from "../../lib/useAuth";
import { Nav } from "../Nav";

interface Account {
  invoice: string; customer: string; invoice_date: string; shipper_amt: number; hst: number;
  due_date: string; pmt_revd: string; carrier_name: string; carrier_in: string; carrier_due: string;
  carrier_amount: number; currency: string; status: string; remarks: string; margin: number | null;
}
interface Stage { stage: string; label: string; done: boolean; detail?: string }
interface Result {
  found: boolean; load: string;
  account?: Account;
  payments_received?: { amount: number; currency: string; check: string; date: string }[];
  ach_entries?: { payee: string; amount: number; currency: string; method: string; date_mailed: string; status: string; mailed: boolean }[];
  carrier_paid?: boolean; customer_paid?: boolean; timeline?: Stage[];
}

const money = (n: number, c = "") => `${c ? c + " " : ""}$${(n || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

function Shell({ children, email, onSignOut }: { children: React.ReactNode; email?: string; onSignOut?: () => void }) {
  return (
    <div className="min-h-screen bg-[#0d0d0d] text-white">
      <div className="max-w-4xl mx-auto px-4 sm:px-6 py-6 sm:py-8">
        <Nav email={email} onSignOut={onSignOut} />
        {children}
      </div>
    </div>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <div className="text-[11px] text-gray-500 uppercase tracking-wide">{label}</div>
      <div className="text-sm text-gray-200 mt-0.5">{value ?? "—"}</div>
    </div>
  );
}

export default function LookupPage() {
  const { authReady, authorized, email, authHeaders, signIn, signOut, signingIn } = useAuth();
  const [q, setQ] = useState("");
  const [data, setData] = useState<Result | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const search = useCallback(async () => {
    const load = q.replace(/\D/g, "");
    if (!load) return;
    setLoading(true); setErr(null); setData(null);
    try {
      const res = await fetch(`${API}/api/load/${load}`, { headers: authHeaders() });
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
      setData(await res.json());
    } catch (e) { setErr(String(e)); }
    setLoading(false);
  }, [q, authHeaders]);

  if (!authReady) return <Shell><p className="text-gray-500">Loading…</p></Shell>;
  if (!authorized)
    return (
      <Shell>
        <div className="text-center py-20">
          <p className="text-gray-400 mb-4">{email ? `${email} is not authorized.` : "Sign in to look up a load."}</p>
          <button onClick={signIn} disabled={signingIn} className="px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-medium">
            {signingIn ? "Redirecting…" : "Sign in with Google"}
          </button>
        </div>
      </Shell>
    );

  const a = data?.account;
  return (
    <Shell email={email} onSignOut={signOut}>
      <h1 className="text-2xl font-bold mb-1">Load lookup</h1>
      <p className="text-sm text-gray-500 mb-5">Type a load number to see its full money trail.</p>

      <div className="flex gap-2 mb-8 max-w-md">
        <input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search()}
          placeholder="e.g. 5563" inputMode="numeric"
          className="flex-1 bg-[#141414] border border-[#242424] rounded-lg px-4 py-2.5 text-sm outline-none focus:border-blue-600" />
        <button onClick={search} disabled={loading}
          className="px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium disabled:opacity-50">
          {loading ? "…" : "Look up"}
        </button>
      </div>

      {err && <p className="text-red-400 text-sm mb-4">Error: {err}</p>}
      {data && !data.found && <p className="text-gray-400">Load {data.load} not found in Accounts.</p>}

      {data?.found && a && (
        <div className="space-y-6">
          {/* Header */}
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
            <h2 className="text-xl font-bold">Load {data.load}</h2>
            <span className="text-gray-400">{a.customer}</span>
            {a.status && <span className="text-xs px-2 py-0.5 rounded-full bg-[#222] text-gray-400">{a.status}</span>}
            <span className="text-gray-600 text-sm">{a.currency}</span>
          </div>

          {/* Timeline */}
          <div className="flex flex-wrap gap-2">
            {data.timeline?.map((s) => (
              <div key={s.stage}
                className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-sm ${s.done ? "border-green-800/50 bg-green-950/20 text-green-300" : "border-[#2a2a2a] bg-[#141414] text-gray-500"}`}>
                <span>{s.done ? "✓" : "○"}</span>
                <span>{s.label}</span>
                {s.detail && <span className="text-xs opacity-70">· {s.detail}</span>}
              </div>
            ))}
          </div>

          {/* Money */}
          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <div className="bg-[#141414] border border-[#242424] rounded-xl p-4">
              <div className="text-xs text-gray-500 uppercase">Shipper (AR)</div>
              <div className="text-xl font-bold text-sky-400 mt-1">{money(a.shipper_amt, a.currency)}</div>
              <div className="text-xs text-gray-500 mt-1">HST {money(a.hst)} · inv {a.invoice}</div>
            </div>
            <div className="bg-[#141414] border border-[#242424] rounded-xl p-4">
              <div className="text-xs text-gray-500 uppercase">Carrier (AP)</div>
              <div className="text-xl font-bold text-orange-400 mt-1">{money(a.carrier_amount, a.currency)}</div>
              <div className="text-xs text-gray-500 mt-1">{a.carrier_name || "—"}</div>
            </div>
            <div className="bg-[#141414] border border-[#242424] rounded-xl p-4">
              <div className="text-xs text-gray-500 uppercase">Margin</div>
              <div className="text-xl font-bold text-green-400 mt-1">{a.margin != null ? money(a.margin, a.currency) : "—"}</div>
              <div className="text-xs text-gray-500 mt-1">
                {a.margin != null && a.shipper_amt ? `${((a.margin / a.shipper_amt) * 100).toFixed(1)}%` : ""}
              </div>
            </div>
            <div className="bg-[#141414] border border-[#242424] rounded-xl p-4">
              <div className="text-xs text-gray-500 uppercase">Status</div>
              <div className="text-sm mt-1">
                <div className={data.customer_paid ? "text-green-400" : "text-gray-500"}>{data.customer_paid ? "✓ Customer paid" : "○ Customer unpaid"}</div>
                <div className={data.carrier_paid ? "text-green-400" : "text-gray-500"}>{data.carrier_paid ? "✓ Carrier paid" : "○ Carrier unpaid"}</div>
              </div>
            </div>
          </div>

          {/* Details grid */}
          <div className="bg-[#141414] border border-[#242424] rounded-xl p-5 grid grid-cols-2 sm:grid-cols-4 gap-4">
            <Field label="Invoice date" value={a.invoice_date} />
            <Field label="Customer due" value={a.due_date} />
            <Field label="Customer paid on" value={a.pmt_revd?.startsWith("#") ? "—" : a.pmt_revd} />
            <Field label="Carrier invoice #" value={a.carrier_in || "—"} />
            <Field label="Carrier due" value={a.carrier_due} />
            <Field label="Remarks" value={a.remarks || "—"} />
          </div>

          {/* Payment rows */}
          {(data.payments_received?.length ?? 0) > 0 && (
            <div>
              <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wide mb-2">Customer payments</h3>
              <div className="rounded-xl border border-[#222] overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-[#151515] text-gray-500 text-xs"><tr>
                    <th className="text-left px-4 py-2">Date</th><th className="text-left px-4 py-2">Check</th>
                    <th className="text-right px-4 py-2">Amount</th></tr></thead>
                  <tbody>{data.payments_received!.map((p, i) => (
                    <tr key={i} className="border-t border-[#1c1c1c]">
                      <td className="px-4 py-2 text-gray-400">{p.date}</td>
                      <td className="px-4 py-2 text-gray-400">{p.check}</td>
                      <td className="px-4 py-2 text-right text-gray-200">{money(p.amount, p.currency)}</td>
                    </tr>))}</tbody>
                </table>
              </div>
            </div>
          )}
          {(data.ach_entries?.length ?? 0) > 0 && (
            <div>
              <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wide mb-2">Carrier payment (ACH/Check)</h3>
              <div className="rounded-xl border border-[#222] overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-[#151515] text-gray-500 text-xs"><tr>
                    <th className="text-left px-4 py-2">Payee</th><th className="text-left px-4 py-2">Method</th>
                    <th className="text-left px-4 py-2">Mailed</th><th className="text-right px-4 py-2">Amount</th></tr></thead>
                  <tbody>{data.ach_entries!.map((e, i) => (
                    <tr key={i} className="border-t border-[#1c1c1c]">
                      <td className="px-4 py-2 text-gray-300 truncate max-w-[240px]">{e.payee}</td>
                      <td className="px-4 py-2 text-gray-400">{e.method || "—"}</td>
                      <td className="px-4 py-2 text-gray-400">{e.date_mailed || "not yet"}</td>
                      <td className="px-4 py-2 text-right text-gray-200">{money(e.amount, e.currency)}</td>
                    </tr>))}</tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </Shell>
  );
}
