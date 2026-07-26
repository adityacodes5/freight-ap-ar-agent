"use client";

import { useMemo, useState } from "react";
import { useAuth, API } from "../../../lib/useAuth";
import { Nav } from "../../Nav";
import { AnalyticsTabs } from "../../AnalyticsTabs";

const MONTHS = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];

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

export default function StatementsPage() {
  const { authReady, authorized, email, authHeaders, signIn, signOut, signingIn } = useAuth();
  // default to the last completed month
  const now = new Date();
  const defY = now.getMonth() === 0 ? now.getFullYear() - 1 : now.getFullYear();
  const defM = now.getMonth() === 0 ? 12 : now.getMonth(); // getMonth() is 0-based → last month
  const [year, setYear] = useState(defY);
  const [month, setMonth] = useState(defM);
  const [busy, setBusy] = useState<"" | "download" | "email">("");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const years = useMemo(() => {
    const y = now.getFullYear();
    return [y, y - 1, y - 2];
  }, [now]);

  const download = async () => {
    setBusy("download"); setErr(null); setMsg(null);
    try {
      const res = await fetch(`${API}/api/statement/download?year=${year}&month=${month}`, { headers: authHeaders() });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? res.statusText);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `Demo_Statement_${year}_${String(month).padStart(2, "0")}.pdf`;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    } catch (e) { setErr(String(e)); }
    setBusy("");
  };

  const emailIt = async () => {
    setBusy("email"); setErr(null); setMsg(null);
    try {
      const res = await fetch(`${API}/api/statement/email?year=${year}&month=${month}`, { method: "POST", headers: authHeaders() });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? res.statusText);
      setMsg(`Emailed ${MONTHS[month - 1]} ${year} statement to ${data.to}.`);
    } catch (e) { setErr(String(e)); }
    setBusy("");
  };

  if (!authReady) return <Shell><p className="text-gray-500">Loading…</p></Shell>;
  if (!authorized)
    return (
      <Shell>
        <div className="text-center py-20">
          <p className="text-gray-400 mb-4">{email ? `${email} is not authorized.` : "Sign in to generate statements."}</p>
          <button onClick={signIn} disabled={signingIn} className="px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-medium">
            {signingIn ? "Redirecting…" : "Sign in with Google"}
          </button>
        </div>
      </Shell>
    );

  return (
    <Shell email={email} onSignOut={signOut}>
      <AnalyticsTabs />
      <h1 className="text-2xl font-bold mb-1">Statements</h1>
      <p className="text-sm text-gray-500 mb-6">
        Generate the bank-style monthly cash statement (PDF) — CAD/USD credits &amp; debits, margin, outstanding.
        Auto-sent 7 days after each month ends.
      </p>

      <div className="bg-[#141414] border border-[#242424] rounded-2xl p-6 max-w-lg">
        <div className="flex flex-wrap gap-3 mb-5">
          <div>
            <label className="block text-xs text-gray-500 mb-1">Month</label>
            <select value={month} onChange={(e) => setMonth(Number(e.target.value))}
              className="bg-[#0d0d0d] border border-[#2a2a2a] rounded-lg px-3 py-2 text-sm outline-none focus:border-blue-600">
              {MONTHS.map((m, i) => <option key={m} value={i + 1}>{m}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Year</label>
            <select value={year} onChange={(e) => setYear(Number(e.target.value))}
              className="bg-[#0d0d0d] border border-[#2a2a2a] rounded-lg px-3 py-2 text-sm outline-none focus:border-blue-600">
              {years.map((y) => <option key={y} value={y}>{y}</option>)}
            </select>
          </div>
        </div>

        <div className="flex flex-wrap gap-3">
          <button onClick={download} disabled={!!busy}
            className="px-5 py-2.5 rounded-lg bg-white text-black hover:bg-gray-200 text-sm font-semibold disabled:opacity-50">
            {busy === "download" ? "Generating…" : "⬇ Download PDF"}
          </button>
          <button onClick={emailIt} disabled={!!busy}
            className="px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium disabled:opacity-50">
            {busy === "email" ? "Sending…" : "✉ Email it to me"}
          </button>
        </div>

        {msg && <p className="text-green-400 text-sm mt-4">✓ {msg}</p>}
        {err && <p className="text-red-400 text-sm mt-4">Error: {err}</p>}
      </div>
    </Shell>
  );
}
