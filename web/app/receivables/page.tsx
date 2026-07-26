"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useAuth, API } from "../../lib/useAuth";
import { Nav } from "../Nav";

interface LoadInfo {
  invoice: string; customer: string; currency: string;
  shipper_amt: number; hst: number; expected: number; paid: boolean;
}
type Index = Record<string, LoadInfo>;
interface Line { id: number; load: string; amount: string }
interface ApplyResult {
  written: number; total: number; message: string;
  rows: number[];
  applied: { load: string; amount: number; matched: boolean; already_paid: boolean }[];
}

const money = (n: number) => `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
let nextId = 1;

export default function ReceivablesPage() {
  const { authReady, authorized, email, authHeaders, signIn, signOut, signingIn } = useAuth();
  const [index, setIndex] = useState<Index>({});
  const [loadingIdx, setLoadingIdx] = useState(false);

  const [cheque, setCheque] = useState("");
  const [date, setDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [method, setMethod] = useState("");  // no default — operator must choose
  const [reference, setReference] = useState("");
  const [customer, setCustomer] = useState("");
  const [lines, setLines] = useState<Line[]>([{ id: nextId++, load: "", amount: "" }]);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ApplyResult | null>(null);
  const [undone, setUndone] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const loadIndex = useCallback(async () => {
    setLoadingIdx(true);
    try {
      const res = await fetch(`${API}/api/receivables/index`, { headers: authHeaders() });
      if (res.ok) setIndex((await res.json()).loads ?? {});
    } catch { /* ignore */ }
    setLoadingIdx(false);
  }, [authHeaders]);
  useEffect(() => { if (authorized) loadIndex(); }, [authorized, loadIndex]);

  const info = (load: string): LoadInfo | undefined => index[load.replace(/\D/g, "")];

  // Auto-fill amount (from the sheet) + customer when a recognised load is typed.
  const setLoad = (id: number, load: string) => {
    setLines((ls) => ls.map((l) => {
      if (l.id !== id) return l;
      const i = index[load.replace(/\D/g, "")];
      const amount = i && !l.amount ? String(i.expected) : l.amount;
      if (i && !customer) setCustomer(i.customer);
      return { ...l, load, amount };
    }));
  };
  const setAmount = (id: number, amount: string) =>
    setLines((ls) => ls.map((l) => (l.id === id ? { ...l, amount } : l)));
  const addLine = () => setLines((ls) => [...ls, { id: nextId++, load: "", amount: "" }]);
  const removeLine = (id: number) => setLines((ls) => (ls.length > 1 ? ls.filter((l) => l.id !== id) : ls));

  const valid = lines.filter((l) => l.load.replace(/\D/g, "") && Number(l.amount) > 0);
  const sum = valid.reduce((s, l) => s + Number(l.amount), 0);
  const chequeN = Number(cheque);
  const diff = chequeN ? +(chequeN - sum).toFixed(2) : 0;
  const balanced = chequeN > 0 && Math.abs(diff) < 0.01;
  const currencies = Array.from(new Set(valid.map((l) => info(l.load)?.currency).filter(Boolean)));
  const mixedCur = currencies.length > 1;
  const anyPaid = valid.some((l) => info(l.load)?.paid);
  const canSubmit = valid.length > 0 && !mixedCur && !busy && method !== "" && (balanced || !chequeN);

  const submit = async () => {
    setBusy(true); setErr(null); setResult(null);
    try {
      const res = await fetch(`${API}/api/receivables/apply`, {
        method: "POST", headers: authHeaders(),
        body: JSON.stringify({
          date, method, customer, reference,
          lines: valid.map((l) => ({ load: l.load.replace(/\D/g, ""), amount: Number(l.amount), currency: info(l.load)?.currency })),
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? res.statusText);
      setResult(data);
      setUndone(false);
      setLines([{ id: nextId++, load: "", amount: "" }]);
      setCheque(""); setReference("");
      loadIndex(); // refresh paid flags
    } catch (e) { setErr(String(e)); }
    setBusy(false);
  };

  const undoLast = async () => {
    if (!result?.rows?.length) return;
    setBusy(true); setErr(null);
    try {
      const res = await fetch(`${API}/api/receivables/undo`, {
        method: "POST", headers: authHeaders(),
        body: JSON.stringify({ rows: result.rows }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? res.statusText);
      setUndone(true);
      loadIndex();
    } catch (e) { setErr(String(e)); }
    setBusy(false);
  };

  if (!authReady) return <Shell><p className="text-gray-500">Loading…</p></Shell>;
  if (!authorized)
    return (
      <Shell>
        <div className="text-center py-20">
          <p className="text-gray-400 mb-4">{email ? `${email} is not authorized.` : "Sign in to record payments."}</p>
          <button onClick={signIn} disabled={signingIn} className="px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-medium">
            {signingIn ? "Redirecting…" : "Sign in with Google"}
          </button>
        </div>
      </Shell>
    );

  return (
    <Shell email={email} onSignOut={signOut}>
      <div className="mb-6">
        <h1 className="text-2xl font-bold">Receivables — apply a payment</h1>
        <p className="text-sm text-gray-500">
          Split a cheque across loads. Each amount is checked against the invoiced total on the sheet, then written to Payment Received (marks the load paid).
        </p>
      </div>

      {result && (
        <div className={`rounded-xl border p-4 mb-6 ${undone ? "border-[#333] bg-[#141414]" : "border-green-800/50 bg-green-950/20"}`}>
          <div className="flex items-start justify-between gap-4">
            <p className={undone ? "text-gray-400 font-medium" : "text-green-300 font-medium"}>
              {undone ? "↩ Reversed — that entry was removed and the load(s) are unpaid again." : `✓ ${result.message}`}
            </p>
            {!undone && result.rows?.length > 0 && (
              <button onClick={undoLast} disabled={busy}
                className="shrink-0 px-3 py-1.5 rounded-lg text-xs font-medium border border-[#3a3a3a] text-gray-300 hover:bg-[#1e1e1e] disabled:opacity-50">
                ↩ Undo last entry
              </button>
            )}
          </div>
          {!undone && (
            <div className="text-xs text-gray-400 mt-2 flex flex-wrap gap-x-4 gap-y-1">
              {result.applied.map((a) => (
                <span key={a.load}>{a.load}: {money(a.amount)}{a.matched ? "" : " ⚠ off-invoice"}{a.already_paid ? " (was already paid!)" : ""}</span>
              ))}
            </div>
          )}
        </div>
      )}
      {err && <p className="text-red-400 text-sm mb-4">Error: {err}</p>}

      <div className="grid lg:grid-cols-[1fr_320px] gap-6">
        {/* Cheque + lines */}
        <div className="space-y-4">
          <div className="bg-[#141414] border border-[#242424] rounded-xl p-5 grid sm:grid-cols-2 gap-4">
            <Field label="Cheque / total amount">
              <input type="number" value={cheque} onChange={(e) => setCheque(e.target.value)} placeholder="8000"
                className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-lg px-3 py-2 text-white outline-none focus:border-blue-600" />
            </Field>
            <Field label="Date received">
              <input type="date" value={date} onChange={(e) => setDate(e.target.value)}
                className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-lg px-3 py-2 text-white outline-none focus:border-blue-600" />
            </Field>
            <Field label="Method">
              <select value={method} onChange={(e) => setMethod(e.target.value)}
                className={`w-full bg-[#0d0d0d] border rounded-lg px-3 py-2 text-white outline-none focus:border-blue-600 ${method ? "border-[#2a2a2a]" : "border-red-500/70"}`}>
                <option value="">Choose…</option>
                <option value="EFT">EFT</option>
                <option value="Cheque-CAD">Cheque-CAD</option>
                <option value="Cheque-USD">Cheque-USD</option>
                <option value="Wire">Wire</option>
                <option value="ACH">ACH</option>
              </select>
            </Field>
            <Field label="Reference / cheque #">
              <input value={reference} onChange={(e) => setReference(e.target.value)} placeholder="optional"
                className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-lg px-3 py-2 text-white outline-none focus:border-blue-600" />
            </Field>
            <Field label="Customer" full>
              <input value={customer} onChange={(e) => setCustomer(e.target.value)} placeholder="auto-filled from first load"
                className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-lg px-3 py-2 text-white outline-none focus:border-blue-600" />
            </Field>
          </div>

          <div className="bg-[#141414] border border-[#242424] rounded-xl p-5">
            <div className="grid grid-cols-[1fr_1fr_auto] gap-3 text-xs text-gray-500 mb-2 px-1">
              <span>Load #</span><span>Amount applied</span><span></span>
            </div>
            <div className="space-y-2">
              {lines.map((l) => {
                const i = info(l.load);
                const amt = Number(l.amount);
                const matched = i && Math.abs(amt - i.expected) < 0.01;
                return (
                  <div key={l.id}>
                    <div className="grid grid-cols-[1fr_1fr_auto] gap-3 items-center">
                      <input value={l.load} onChange={(e) => setLoad(l.id, e.target.value)} placeholder="5001"
                        className="bg-[#0d0d0d] border border-[#2a2a2a] rounded-lg px-3 py-2 text-white outline-none focus:border-blue-600" />
                      <input type="number" value={l.amount} onChange={(e) => setAmount(l.id, e.target.value)} placeholder="0.00"
                        className={`bg-[#0d0d0d] border rounded-lg px-3 py-2 text-white outline-none focus:border-blue-600 ${i && !matched ? "border-amber-700" : "border-[#2a2a2a]"}`} />
                      <button onClick={() => removeLine(l.id)} className="text-gray-600 hover:text-red-400 px-2" title="remove">✕</button>
                    </div>
                    {l.load.replace(/\D/g, "") && (
                      <div className="text-xs mt-1 px-1">
                        {i ? (
                          <span className={matched ? "text-gray-500" : "text-amber-400"}>
                            {i.customer} · {i.currency} · sheet says <b>{money(i.expected)}</b>
                            {matched ? " ✓" : ` — you entered ${money(amt || 0)}`}
                            {i.paid && <span className="text-red-400"> · ⚠ already marked paid</span>}
                          </span>
                        ) : (
                          <span className="text-gray-600">load not found on the sheet</span>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
            <button onClick={addLine} className="mt-3 text-sm text-blue-400 hover:text-blue-300">+ add load</button>
          </div>
        </div>

        {/* Summary / submit */}
        <div className="bg-[#141414] border border-[#242424] rounded-xl p-5 h-fit lg:sticky lg:top-6">
          <div className="text-sm font-semibold text-gray-300 mb-4">Reconcile</div>
          <Row label="Cheque total" value={chequeN ? money(chequeN) : "—"} />
          <Row label="Split total" value={money(sum)} />
          <div className="border-t border-[#242424] my-3" />
          {chequeN > 0 ? (
            balanced
              ? <p className="text-green-400 font-semibold text-sm">✓ Balanced</p>
              : <p className="text-amber-400 font-semibold text-sm">{diff > 0 ? `${money(diff)} unallocated` : `Over by ${money(-diff)}`}</p>
          ) : <p className="text-gray-500 text-xs">Enter a cheque total to reconcile (optional).</p>}

          {mixedCur && <p className="text-red-400 text-xs mt-3">⚠ Mixed currencies ({currencies.join(", ")}) — one cheque should be a single currency.</p>}
          {anyPaid && <p className="text-amber-400 text-xs mt-3">Some loads are already marked paid — double-check before submitting.</p>}
          {loadingIdx && <p className="text-gray-600 text-xs mt-3">Loading sheet amounts…</p>}

          <button onClick={submit} disabled={!canSubmit}
            className={`w-full mt-4 px-4 py-2.5 rounded-lg font-medium text-sm transition-all ${
              canSubmit ? "bg-green-600 hover:bg-green-500 text-white" : "bg-[#1a1a1a] text-gray-600 cursor-not-allowed border border-[#2a2a2a]"}`}>
            {busy ? "Writing…" : `Record ${valid.length} payment${valid.length === 1 ? "" : "s"} → sheet`}
          </button>
          {!balanced && chequeN > 0 && valid.length > 0 && !mixedCur &&
            <p className="text-xs text-gray-600 mt-2 text-center">Balance the split to enable submit.</p>}
        </div>
      </div>
    </Shell>
  );
}

function Field({ label, children, full }: { label: string; children: React.ReactNode; full?: boolean }) {
  return (
    <div className={full ? "sm:col-span-2" : ""}>
      <label className="text-xs text-gray-500 block mb-1">{label}</label>
      {children}
    </div>
  );
}
function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between text-sm py-1">
      <span className="text-gray-500">{label}</span>
      <span className="text-gray-200">{value}</span>
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
