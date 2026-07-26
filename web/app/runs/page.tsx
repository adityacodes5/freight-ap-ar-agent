"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import type { Session } from "@supabase/supabase-js";
import { supabase, ALLOWED_EMAILS } from "../../lib/supabase";
import { Nav } from "../Nav";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface Stats {
  verified: number;
  discrepancy: number;
  skipped_no_match: number;
  skipped_already_done: number;
  hold?: number;
  errors: number;
}
interface SyncRow {
  row: number; load: number; client: string; shipper: string;
  carrier: string; currency: string; carrier_name: string; cad: boolean;
}
interface SyncResult {
  highest_existing: number; first_empty_row: number; sales_rows: number;
  committed: boolean; count: number; cad_count: number;
  rows: SyncRow[]; message: string;
}
interface AchRow {
  ach_row: number; load: number; currency: string; amount: string;
  payee: string; flagged: boolean; remark: string;
}
interface AchResult {
  committed: boolean; count: number; cad_count: number; usd_count: number;
  flagged_count: number; held_count?: number; held_loads?: number[];
  start_row: number; batch_action: string;
  window_through: string; rows: AchRow[]; message: string;
}
interface Status {
  running: boolean;
  finished_at: string | null;
  stats: Stats | null;
  error: string | null;
}
interface ReviewItem {
  key: string;
  load_no: string;
  type: string;
  status: string;
  reason: string;
  email_from: string;
  email_subject: string;
  sheet_row: number | null;
  details: Record<string, unknown>;
}
interface ReportItem {
  load_no: string; status: string; reason?: string;
  email_subject?: string; email_from?: string; sheet_row?: number | null;
}
interface RunReport {
  run_id: string;
  type: string;
  finished_at: string;
  started_at?: string;
  counts: Record<string, number>;
  items?: ReportItem[];
}
interface PromptItem {
  key: string;
  content: string;
  description: string | null;
  updated_at: string | null;
  updated_by: string | null;
}
interface ChatMsg {
  role: "user" | "assistant";
  content: string;
}

function Spinner() {
  return (
    <svg className="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
    </svg>
  );
}
function PlayIcon() {
  return (
    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
        d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
        d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  );
}
function StatCard({ label, value, color }: { label: string; value: number | string; color: string }) {
  return (
    <div className="bg-[#1a1a1a] border border-[#2a2a2a] rounded-xl p-5 flex flex-col gap-1">
      <span className="text-xs text-gray-500 uppercase tracking-wider">{label}</span>
      <span className={`text-3xl font-bold ${color}`}>{value}</span>
    </div>
  );
}

/** Turns a flagged item's `details` into a plain "what the invoice says vs what
 *  the sheet says" table, with mismatches called out — instead of a raw key dump.
 *  The runners store paired `<field>_invoice` / `<field>_sheet` values plus a few
 *  standalone facts (invoice number, factoring flag, accessorial charges). */
function CompareView({ details, sheetRow }: { details: Record<string, unknown>; sheetRow?: number | null }) {
  const d = details || {};
  const show = (v: unknown) => (v === undefined || v === null || v === "" ? "—" : String(v));
  const norm = (v: unknown) => String(v ?? "").trim().toLowerCase().replace(/\s+/g, " ");
  const LABELS: Record<string, string> = { carrier: "Carrier", customer: "Customer", amount: "Amount", currency: "Currency" };

  const bases = Array.from(
    new Set(Object.keys(d).filter((k) => k.endsWith("_invoice")).map((k) => k.slice(0, -8))),
  );
  const rows = bases.map((b) => {
    const inv = d[`${b}_invoice`];
    const sh = d[`${b}_sheet`];
    return { label: LABELS[b] ?? b.replace(/_/g, " "), inv, sh, mismatch: norm(inv) !== norm(sh) };
  });
  const extras = Object.entries(d).filter(
    ([k]) => !k.endsWith("_invoice") && !k.endsWith("_sheet") && k !== "extra_charges",
  );
  const charges = Array.isArray(d.extra_charges) ? (d.extra_charges as Record<string, unknown>[]) : [];

  return (
    <div className="mt-2 bg-black/20 rounded-lg p-3 text-xs">
      {rows.length > 0 && (
        <table className="w-full border-collapse">
          <thead>
            <tr className="text-left opacity-40">
              <th className="font-normal pb-1.5 pr-3"></th>
              <th className="font-normal pb-1.5 pr-3">Invoice says</th>
              <th className="font-normal pb-1.5">Sheet says</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.label} className={r.mismatch ? "text-red-300" : "text-gray-300"}>
                <td className="opacity-60 pr-3 py-0.5 capitalize">{r.label}</td>
                <td className="font-mono pr-3 py-0.5">{show(r.inv)}</td>
                <td className="font-mono py-0.5">
                  {show(r.sh)}
                  {r.mismatch && <span className="ml-1.5 text-red-400" title="does not match">≠</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {(extras.length > 0 || charges.length > 0 || sheetRow) && (
        <div className="mt-2 pt-2 border-t border-white/10 flex flex-wrap gap-x-4 gap-y-1 text-gray-400">
          {extras.map(([k, v]) => (
            <span key={k}>
              <span className="opacity-50">{k.replace(/_/g, " ")}:</span> <span className="font-mono">{show(v)}</span>
            </span>
          ))}
          {charges.map((c, i) => (
            <span key={i}>
              <span className="opacity-50">{String(c?.label ?? "fee")}:</span>{" "}
              <span className="font-mono">${String(c?.amount ?? 0)}</span>
            </span>
          ))}
          {sheetRow ? (
            <span>
              <span className="opacity-50">sheet row:</span> <span className="font-mono">{sheetRow}</span>
            </span>
          ) : null}
        </div>
      )}
    </div>
  );
}

export default function RunsPage() {
  const [session, setSession] = useState<Session | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [signingIn, setSigningIn] = useState(false);

  const [status, setStatus] = useState<Status | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const [mode, setMode] = useState<"watermark" | "fixed">("watermark");
  const [limit, setLimit] = useState(500);
  const [waking, setWaking] = useState(false);

  const [review, setReview] = useState<ReviewItem[]>([]);
  const [runs, setRuns] = useState<RunReport[]>([]);
  const [prompts, setPrompts] = useState<PromptItem[]>([]);
  const [chat, setChat] = useState<ChatMsg[]>([]);
  const [chatInput, setChatInput] = useState("");
  const [chatBusy, setChatBusy] = useState(false);

  const [sync, setSync] = useState<SyncResult | null>(null);
  const [syncBusy, setSyncBusy] = useState(false);
  const [ach, setAch] = useState<AchResult | null>(null);
  const [achBusy, setAchBusy] = useState(false);
  // Cutoff the operator picks: pay loads due on/before this date (plus overdue).
  const [achThrough, setAchThrough] = useState(() => new Date().toISOString().slice(0, 10));
  const [expanded, setExpanded] = useState<string | null>(null);
  const [openRun, setOpenRun] = useState<string | null>(null);

  const logRef = useRef<HTMLDivElement>(null);
  const chatRef = useRef<HTMLDivElement>(null);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight; }, [logs]);
  useEffect(() => { if (chatRef.current) chatRef.current.scrollTop = chatRef.current.scrollHeight; }, [chat]);

  // ── Auth: Google sign-in via Supabase ──────────────────────────────────────
  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session);
      setAuthReady(true);
    });
    const { data: sub } = supabase.auth.onAuthStateChange((_e, s) => setSession(s));
    return () => sub.subscription.unsubscribe();
  }, []);

  const token = session?.access_token;
  const email = (session?.user?.email ?? "").toLowerCase();
  const authorized = !!session && ALLOWED_EMAILS.includes(email);

  const authHeaders = useCallback((): Record<string, string> => ({
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }), [token]);

  const signIn = async () => {
    setSigningIn(true);
    await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: typeof window !== "undefined" ? window.location.origin : undefined },
    });
  };
  const signOut = async () => { await supabase.auth.signOut(); setSession(null); };

  const refresh = useCallback(async () => {
    try {
      const [s, r, h, p] = await Promise.all([
        fetch(`${API}/api/status`, { headers: authHeaders() }),
        fetch(`${API}/api/review`, { headers: authHeaders() }),
        fetch(`${API}/api/runs`, { headers: authHeaders() }),
        fetch(`${API}/api/prompts`, { headers: authHeaders() }),
      ]);
      if (s.ok) setStatus(await s.json());
      if (r.ok) setReview((await r.json()).items ?? []);
      if (h.ok) setRuns((await h.json()).runs ?? []);
      if (p.ok) setPrompts((await p.json()).prompts ?? []);
    } catch {}
  }, [authHeaders]);

  useEffect(() => { if (authorized) refresh(); }, [authorized, refresh]);

  // endpoint = "run" | "run-carrier" | "run-all".
  // Default is watermark mode (everything since the last run) — the same logic the
  // cron uses. Only when the user opts into a fixed count do we send ?limit=N.
  const handleRun = async (endpoint: "run" | "run-carrier" | "run-all") => {
    setLogs([]); setRunning(true); setWaking(true);
    try { await fetch(`${API}/api/health`, { headers: authHeaders() }); } catch {}
    setWaking(false);
    const qs = mode === "fixed" ? `?limit=${limit}` : "";
    try {
      const res = await fetch(`${API}/api/${endpoint}${qs}`, { method: "POST", headers: authHeaders() });
      if (!res.ok) { setLogs([`Error: ${(await res.json()).detail}`]); setRunning(false); return; }
    } catch (e) { setLogs([`Could not reach server: ${e}`]); setRunning(false); return; }

    if (esRef.current) esRef.current.close();
    const es = new EventSource(`${API}/api/stream?access_token=${token}`);
    esRef.current = es;
    es.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === "log") setLogs((p) => [...p, data.line]);
      else if (data.type === "done") { es.close(); setRunning(false); refresh(); }
    };
    es.onerror = () => { es.close(); setRunning(false); refresh(); };
  };

  const sendChat = async () => {
    const text = chatInput.trim();
    if (!text || chatBusy) return;
    const newChat: ChatMsg[] = [...chat, { role: "user", content: text }];
    setChat(newChat); setChatInput(""); setChatBusy(true);
    try {
      const res = await fetch(`${API}/api/review/chat`, {
        method: "POST", headers: authHeaders(),
        body: JSON.stringify({ messages: newChat }),
      });
      const data = await res.json();
      setChat([...newChat, { role: "assistant", content: data.reply ?? "(no reply)" }]);
      refresh(); // sheet may have changed
    } catch (e) {
      setChat([...newChat, { role: "assistant", content: `Error: ${e}` }]);
    }
    setChatBusy(false);
  };

  const resolveItem = async (key: string) => {
    await fetch(`${API}/api/review/resolve?key=${encodeURIComponent(key)}`, { method: "POST", headers: authHeaders() });
    refresh();
  };

  const handleSync = async (commit: boolean) => {
    setSyncBusy(true);
    try {
      const res = await fetch(`${API}/api/sync-sales?commit=${commit}`, { method: "POST", headers: authHeaders() });
      const data = await res.json();
      if (!res.ok) { setSync({ ...(sync as SyncResult), message: `Error: ${data.detail}` } as SyncResult); }
      else setSync(data);
    } catch (e) {
      setSync({ message: `Could not reach server: ${e}` } as SyncResult);
    }
    setSyncBusy(false);
  };

  // Payments: populate the ACH/Check sheet from due-to-pay loads. Manual, web-only.
  const handleAch = async (commit: boolean) => {
    setAchBusy(true);
    try {
      const res = await fetch(`${API}/api/sync-ach?commit=${commit}&through=${achThrough}`, { method: "POST", headers: authHeaders() });
      const data = await res.json();
      if (!res.ok) { setAch({ ...(ach as AchResult), message: `Error: ${data.detail}` } as AchResult); }
      else setAch(data);
    } catch (e) {
      setAch({ message: `Could not reach server: ${e}` } as AchResult);
    }
    setAchBusy(false);
  };

  const stats = status?.stats;
  const lastRun = status?.finished_at ? new Date(status.finished_at).toLocaleString() : "Never";

  const statusColor = (s: string) =>
    s === "discrepancy" ? "text-red-400 bg-red-950/40 border-red-800/50"
    : s === "hold" ? "text-amber-300 bg-amber-950/40 border-amber-700/50"
    : s === "error" ? "text-orange-400 bg-orange-950/40 border-orange-800/50"
    : "text-yellow-400 bg-yellow-950/30 border-yellow-800/40";

  // ── Auth gate ──────────────────────────────────────────────────────────────
  if (!authReady) {
    return (
      <main className="min-h-screen bg-[#0d0d0d] text-gray-400 flex items-center justify-center">
        <Spinner />
      </main>
    );
  }
  if (!authorized) {
    return (
      <main className="min-h-screen bg-[#0d0d0d] text-gray-200 flex items-center justify-center px-6">
        <div className="w-full max-w-sm bg-[#141414] border border-[#222] rounded-2xl p-8 text-center space-y-5">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">Demo Logistics</h1>
            <p className="text-sm text-gray-500 mt-1">Invoice Processor</p>
          </div>
          {session ? (
            <>
              <p className="text-sm text-red-400">
                <span className="font-mono">{email}</span> isn&apos;t authorized to use this dashboard.
              </p>
              <button onClick={signOut}
                className="w-full px-4 py-2.5 rounded-lg bg-[#222] hover:bg-[#2a2a2a] text-sm font-medium">
                Sign out
              </button>
            </>
          ) : (
            <>
              <p className="text-sm text-gray-400">Sign in with your authorized Google account to continue.</p>
              <button onClick={signIn} disabled={signingIn}
                className="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg bg-white text-gray-800 hover:bg-gray-100 text-sm font-semibold disabled:opacity-60">
                {signingIn ? <Spinner /> : (
                  <svg className="w-4 h-4" viewBox="0 0 24 24"><path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1Z"/><path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84A11 11 0 0 0 12 23Z"/><path fill="#FBBC05" d="M5.84 14.1a6.6 6.6 0 0 1 0-4.2V7.06H2.18a11 11 0 0 0 0 9.88l3.66-2.84Z"/><path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1A11 11 0 0 0 2.18 7.06l3.66 2.84C6.71 7.31 9.14 5.38 12 5.38Z"/></svg>
                )}
                Sign in with Google
              </button>
            </>
          )}
        </div>
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-[#0d0d0d] text-gray-200">
      <div className="max-w-6xl mx-auto px-6 py-8">
        <Nav email={email} onSignOut={signOut} />
        <div className="flex items-center justify-between mb-8">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Demo Logistics</h1>
            <p className="text-sm text-gray-500 mt-0.5">Invoice Processor</p>
          </div>
          <div className="flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${running ? "bg-blue-400 animate-pulse" : "bg-green-500"}`} />
            <span className="text-sm text-gray-400">{running ? "Running…" : "Idle"}</span>
          </div>
        </div>

        <div className="space-y-8">
        {/* Stats */}
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
          <StatCard label="Verified" value={stats?.verified ?? "—"} color="text-green-400" />
          <StatCard label="Discrepancy" value={stats?.discrepancy ?? "—"} color="text-red-400" />
          <StatCard label="On Hold" value={stats?.hold ?? "—"} color="text-amber-300" />
          <StatCard label="No Match" value={stats?.skipped_no_match ?? "—"} color="text-yellow-400" />
          <StatCard label="Errors" value={stats?.errors ?? "—"} color="text-orange-400" />
        </div>
        {status?.finished_at && <p className="text-xs text-gray-600">Last run: {lastRun}</p>}

        {/* Controls */}
        <div className="space-y-3">
          {/* Scan-window selector: default = everything since the last run (cron parity) */}
          <div className="flex flex-wrap items-center gap-3">
            <div className="inline-flex rounded-lg border border-[#2a2a2a] bg-[#1a1a1a] p-0.5 text-sm">
              <button onClick={() => setMode("watermark")} disabled={running}
                className={`px-3 py-1.5 rounded-md transition-all ${mode === "watermark"
                  ? "bg-[#2b2b2b] text-white" : "text-gray-500 hover:text-gray-300"}`}>
                Since last run
              </button>
              <button onClick={() => setMode("fixed")} disabled={running}
                className={`px-3 py-1.5 rounded-md transition-all ${mode === "fixed"
                  ? "bg-[#2b2b2b] text-white" : "text-gray-500 hover:text-gray-300"}`}>
                Last N emails
              </button>
            </div>
            {mode === "fixed" ? (
              <div className="flex items-center gap-2 bg-[#1a1a1a] border border-[#2a2a2a] rounded-lg px-3 py-2">
                <span className="text-sm text-gray-500">Emails</span>
                <input type="number" value={limit} min={10} max={1000} step={50}
                  onChange={(e) => setLimit(Number(e.target.value))} disabled={running}
                  className="w-20 bg-transparent text-white text-sm text-right outline-none" />
              </div>
            ) : (
              <span className="text-xs text-gray-500">
                Processes every email since the last run (same as the daily 6 pm run) — no count needed.
              </span>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-4">
            {/* RUN ALL — sync + shipper + carrier in one go (the full daily job) */}
            <button onClick={() => handleRun("run-all")} disabled={running || syncBusy}
              className={`flex items-center gap-2 px-5 py-2.5 rounded-lg font-semibold text-sm transition-all
                ${running || syncBusy ? "bg-[#1a1a1a] text-gray-500 cursor-not-allowed border border-[#2a2a2a]"
                : "bg-white text-black hover:bg-gray-200 shadow-lg shadow-black/30"}`}>
              {running && waking ? <><Spinner /> Waking server…</> : running ? <><Spinner /> Running…</> : <>▶▶ Run All</>}
            </button>
            <button onClick={() => handleRun("run")} disabled={running}
              className={`flex items-center gap-2 px-5 py-2.5 rounded-lg font-medium text-sm transition-all
                ${running ? "bg-[#1a1a1a] text-gray-500 cursor-not-allowed border border-[#2a2a2a]"
                : "bg-blue-600 hover:bg-blue-500 text-white shadow-lg shadow-blue-900/30"}`}>
              {running ? <><Spinner /> Processing…</> : <><PlayIcon /> Shipper Invoices</>}
            </button>
            <button onClick={() => handleRun("run-carrier")} disabled={running}
              className={`flex items-center gap-2 px-5 py-2.5 rounded-lg font-medium text-sm transition-all
                ${running ? "bg-[#1a1a1a] text-gray-500 cursor-not-allowed border border-[#2a2a2a]"
                : "bg-violet-600 hover:bg-violet-500 text-white shadow-lg shadow-violet-900/30"}`}>
              {running ? <><Spinner /> Processing…</> : <><PlayIcon /> Carrier Invoices</>}
            </button>
            <button onClick={() => handleSync(false)} disabled={syncBusy || running}
              className={`flex items-center gap-2 px-5 py-2.5 rounded-lg font-medium text-sm transition-all
                ${syncBusy ? "bg-[#1a1a1a] text-gray-500 cursor-not-allowed border border-[#2a2a2a]"
                : "bg-emerald-700 hover:bg-emerald-600 text-white shadow-lg shadow-emerald-900/30"}`}>
              {syncBusy ? <><Spinner /> Checking…</> : <>↧ Sync new loads</>}
            </button>
          </div>
        </div>

        {/* ── SALES → ACCOUNTS SYNC ────────────────────────────────── */}
        {sync && (
          <div className="rounded-xl border border-emerald-900/50 bg-emerald-950/10 p-4">
            <div className="flex items-center justify-between gap-3 mb-2">
              <p className="text-sm">
                <span className="font-semibold text-emerald-300">Sales → Accounts.</span>{" "}
                <span className="text-gray-300">{sync.message}</span>
              </p>
              {sync.count > 0 && !sync.committed && (
                <button onClick={() => handleSync(true)} disabled={syncBusy}
                  className="text-xs px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-medium whitespace-nowrap disabled:opacity-40">
                  {syncBusy ? "Writing…" : `Add ${sync.count} to sheet`}
                </button>
              )}
            </div>
            {sync.rows?.length > 0 && (
              <div className="max-h-56 overflow-y-auto rounded-lg border border-[#222]">
                <table className="w-full text-xs">
                  <thead className="bg-[#151515] text-gray-500 sticky top-0">
                    <tr>
                      <th className="text-left px-3 py-1.5 font-medium">Load</th>
                      <th className="text-left px-3 py-1.5 font-medium">Client</th>
                      <th className="text-right px-3 py-1.5 font-medium">Shipper</th>
                      <th className="text-right px-3 py-1.5 font-medium">Carrier</th>
                      <th className="text-left px-3 py-1.5 font-medium">Cur</th>
                      <th className="text-left px-3 py-1.5 font-medium">Carrier name</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sync.rows.map((r) => (
                      <tr key={r.load} className="border-t border-[#1c1c1c]">
                        <td className="px-3 py-1.5 text-gray-300">{r.load}</td>
                        <td className="px-3 py-1.5 text-gray-300 truncate max-w-[160px]">{r.client}</td>
                        <td className={`px-3 py-1.5 text-right ${r.cad ? "text-amber-300" : "text-gray-300"}`}>{r.shipper || "—"}</td>
                        <td className={`px-3 py-1.5 text-right ${r.cad ? "text-amber-300" : "text-gray-300"}`}>{r.carrier || "—"}</td>
                        <td className="px-3 py-1.5 text-gray-400">{r.currency}</td>
                        <td className="px-3 py-1.5 text-gray-400 truncate max-w-[180px]">{r.carrier_name}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {sync.cad_count > 0 && !sync.committed && (
              <p className="text-[11px] text-amber-400/70 mt-2">
                {sync.cad_count} CAD load(s) — shipper/carrier amounts will be highlighted yellow on write.
              </p>
            )}
          </div>
        )}

        {/* ── PAYMENTS: ACH/Check (manual, web-only — NOT part of Run All/cron) ── */}
        <div className="rounded-xl border border-fuchsia-900/40 bg-fuchsia-950/10 p-4 space-y-3">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div>
              <p className="text-sm font-semibold text-fuchsia-300">Payments → ACH/Check</p>
              <p className="text-xs text-gray-500">
                Queue loads that are due to pay (carrier invoice in, due on/before the date you pick, plus overdue).
                Carrier Payment Issues are held out; discrepancies are added but flagged magenta.
                Manual &amp; separate — not part of Run All or the daily run.
              </p>
            </div>
            <div className="flex items-center gap-2">
              <label className="text-xs text-gray-400 whitespace-nowrap">Pay through</label>
              <input type="date" value={achThrough} onChange={(e) => setAchThrough(e.target.value)}
                disabled={achBusy}
                className="bg-[#151515] border border-[#2a2a2a] rounded-lg px-2.5 py-2 text-sm text-gray-200
                  focus:outline-none focus:border-fuchsia-600 disabled:opacity-40 [color-scheme:dark]" />
              <button onClick={() => handleAch(false)} disabled={achBusy}
                className={`flex items-center gap-2 px-5 py-2.5 rounded-lg font-medium text-sm transition-all
                  ${achBusy ? "bg-[#1a1a1a] text-gray-500 cursor-not-allowed border border-[#2a2a2a]"
                  : "bg-fuchsia-700 hover:bg-fuchsia-600 text-white shadow-lg shadow-fuchsia-900/30"}`}>
                {achBusy ? <><Spinner /> Checking…</> : <>💳 Preview payments</>}
              </button>
            </div>
          </div>
          {ach && (
            <>
              <div className="flex items-center justify-between gap-3">
                <p className="text-sm text-gray-300">{ach.message}</p>
                {ach.count > 0 && !ach.committed && (
                  <button onClick={() => handleAch(true)} disabled={achBusy}
                    className="text-xs px-3 py-1.5 rounded-lg bg-fuchsia-600 hover:bg-fuchsia-500 text-white font-medium whitespace-nowrap disabled:opacity-40">
                    {achBusy ? "Writing…" : `Write ${ach.count} to ACH/Check`}
                  </button>
                )}
              </div>
              {ach.rows?.length > 0 && (
                <div className="max-h-72 overflow-y-auto rounded-lg border border-[#222]">
                  <table className="w-full text-xs">
                    <thead className="bg-[#151515] text-gray-500 sticky top-0">
                      <tr>
                        <th className="text-left px-3 py-1.5 font-medium">Load</th>
                        <th className="text-left px-3 py-1.5 font-medium">Cur</th>
                        <th className="text-right px-3 py-1.5 font-medium">Amount</th>
                        <th className="text-left px-3 py-1.5 font-medium">Payee</th>
                        <th className="text-left px-3 py-1.5 font-medium">Note</th>
                      </tr>
                    </thead>
                    <tbody>
                      {ach.rows.map((r) => (
                        <tr key={r.load} className={`border-t border-[#1c1c1c] ${r.flagged ? "bg-fuchsia-950/30" : ""}`}>
                          <td className="px-3 py-1.5 text-gray-300">{r.load}</td>
                          <td className="px-3 py-1.5 text-gray-400">{r.currency}</td>
                          <td className="px-3 py-1.5 text-right text-gray-300">{r.amount || "—"}</td>
                          <td className="px-3 py-1.5 text-gray-400 truncate max-w-[220px]">{r.payee}</td>
                          <td className="px-3 py-1.5 text-fuchsia-300 truncate max-w-[220px]">{r.flagged ? (r.remark || "flagged") : ""}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {ach.count > 0 && (
                <p className="text-[11px] text-gray-500">
                  {ach.batch_action} · {ach.cad_count} CAD, {ach.usd_count} USD · {ach.flagged_count} flagged magenta · due on/before {ach.window_through}
                </p>
              )}
              {(ach.held_count ?? 0) > 0 && (
                <p className="text-[11px] text-amber-400">
                  ⚠ Held out {ach.held_count} Carrier Payment Issue load(s) — not queued for payment
                  {ach.held_loads?.length ? `: ${ach.held_loads.join(", ")}` : ""}.
                </p>
              )}
            </>
          )}
        </div>

        {/* Live log */}
        {(running || logs.length > 0) && (
          <div className="rounded-xl overflow-hidden border border-[#2a2a2a]">
            <div className="bg-[#151515] px-4 py-2 flex items-center gap-2 border-b border-[#2a2a2a]">
              <span className="w-3 h-3 rounded-full bg-red-500/70" />
              <span className="w-3 h-3 rounded-full bg-yellow-500/70" />
              <span className="w-3 h-3 rounded-full bg-green-500/70" />
              <span className="ml-3 text-xs text-gray-500 font-mono">live log</span>
            </div>
            <div ref={logRef} className="bg-[#0a0a0a] font-mono text-xs p-4 h-72 overflow-y-auto space-y-0.5">
              {logs.map((line, i) => {
                const c = line.includes("──") || line.includes("Scanning") ? "text-blue-300 font-semibold"
                  : line.includes("✓") ? "text-green-400" : line.includes("⚠") ? "text-yellow-400"
                  : line.includes("✗") ? "text-red-400" : line.includes("→") ? "text-gray-500" : "text-gray-300";
                return <div key={i} className={`leading-5 ${c}`}>{line}</div>;
              })}
              {running && <div className="text-blue-400 animate-pulse">▊</div>}
            </div>
          </div>
        )}

        {/* ── NEEDS REVIEW ─────────────────────────────────────────── */}
        <div>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400">
              Needs Review {review.length > 0 && <span className="ml-2 text-red-400">({review.length})</span>}
            </h2>
            <button onClick={refresh} className="text-xs text-gray-500 hover:text-gray-300">↻ refresh</button>
          </div>
          {review.length === 0 ? (
            <div className="bg-[#141414] border border-[#222] rounded-xl p-6 text-center text-sm text-gray-600">
              Nothing to review — all clear ✓
            </div>
          ) : (
            <div className="space-y-2">
              {review.map((it) => (
                <div key={it.key} className={`rounded-xl border p-4 ${statusColor(it.status)}`}>
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-1">
                        <span className="font-bold">Load {it.load_no}</span>
                        <span className="text-[10px] uppercase tracking-wide px-2 py-0.5 rounded-full bg-black/30">
                          {it.type}
                        </span>
                        <span className="text-[10px] uppercase tracking-wide opacity-70">{it.status}</span>
                      </div>
                      <p className="text-sm opacity-90">{it.reason}</p>
                      {it.email_subject && (
                        <p className="text-xs opacity-50 mt-1 truncate">
                          {it.email_from} — {it.email_subject}
                        </p>
                      )}
                      {((it.details && Object.keys(it.details).length > 0) || it.sheet_row) && (
                        <button onClick={() => setExpanded(expanded === it.key ? null : it.key)}
                          className="text-[11px] opacity-60 hover:opacity-100 mt-1.5 underline underline-offset-2">
                          {expanded === it.key ? "hide comparison" : "compare invoice vs sheet"}
                        </button>
                      )}
                      {expanded === it.key && (
                        <CompareView details={it.details} sheetRow={it.sheet_row} />
                      )}
                    </div>
                    <button onClick={() => resolveItem(it.key)}
                      className="text-xs px-3 py-1.5 rounded-lg bg-black/30 hover:bg-black/50 whitespace-nowrap">
                      Mark done
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* ── REVIEW CHAT ──────────────────────────────────────────── */}
        <div>
          <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-3">
            Fix by Chat
          </h2>
          <div className="bg-[#141414] border border-[#222] rounded-xl overflow-hidden">
            <div ref={chatRef} className="h-64 overflow-y-auto p-4 space-y-3">
              {chat.length === 0 ? (
                <p className="text-sm text-gray-600">
                  Type a fix in plain English. e.g.{" "}
                  <span className="text-gray-400">&ldquo;load 5621 carrier amount should be 2100 and clear the flag&rdquo;</span>
                </p>
              ) : chat.map((m, i) => (
                <div key={i} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                  <div className={`max-w-[80%] rounded-2xl px-4 py-2 text-sm whitespace-pre-wrap
                    ${m.role === "user" ? "bg-blue-600 text-white" : "bg-[#222] text-gray-200"}`}>
                    {m.content}
                  </div>
                </div>
              ))}
              {chatBusy && <div className="flex justify-start"><div className="bg-[#222] rounded-2xl px-4 py-2"><Spinner /></div></div>}
            </div>
            <div className="border-t border-[#222] p-3 flex gap-2">
              <input value={chatInput} onChange={(e) => setChatInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && sendChat()}
                placeholder="e.g. load 5507 is correct, clear the flag"
                disabled={chatBusy}
                className="flex-1 bg-[#0a0a0a] border border-[#2a2a2a] rounded-lg px-3 py-2 text-sm outline-none focus:border-blue-600" />
              <button onClick={sendChat} disabled={chatBusy || !chatInput.trim()}
                className="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-sm font-medium">
                Send
              </button>
            </div>
          </div>
        </div>

        {/* ── RUN HISTORY ──────────────────────────────────────────── */}
        {runs.length > 0 && (
          <div>
            <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-3">Recent Runs</h2>
            <div className="space-y-1.5">
              {runs.slice(0, 12).map((r) => {
                const open = openRun === r.run_id;
                const detail = (r.items ?? []).filter((it) =>
                  ["discrepancy", "hold", "no_row", "error", "review"].includes(it.status));
                const verified = (r.items ?? []).filter((it) => it.status === "verified");
                return (
                  <div key={r.run_id} className="bg-[#141414] border border-[#222] rounded-lg overflow-hidden">
                    <button onClick={() => setOpenRun(open ? null : r.run_id)}
                      className="w-full flex items-center justify-between px-4 py-2.5 text-sm hover:bg-[#181818]">
                      <div className="flex items-center gap-3">
                        <span className="text-gray-600 text-xs">{open ? "▾" : "▸"}</span>
                        <span className={`text-[10px] uppercase px-2 py-0.5 rounded-full ${r.type === "carrier" ? "bg-violet-950 text-violet-300" : "bg-blue-950 text-blue-300"}`}>
                          {r.type}
                        </span>
                        <span className="text-gray-500 text-xs">{new Date(r.finished_at).toLocaleString()}</span>
                      </div>
                      <div className="flex items-center gap-3 text-xs">
                        <span className="text-green-400">✓ {r.counts.verified ?? 0}</span>
                        <span className="text-red-400">⚠ {r.counts.discrepancy ?? 0}</span>
                        {(r.counts.hold ?? 0) > 0 && <span className="text-amber-300">⛔ {r.counts.hold}</span>}
                        <span className="text-orange-400">✗ {r.counts.error ?? 0}</span>
                        {r.type === "carrier" && <span className="text-gray-400">↗ {r.counts.forwarded ?? 0}</span>}
                      </div>
                    </button>
                    {open && (
                      <div className="border-t border-[#1f1f1f] px-4 py-3 text-xs space-y-2">
                        {detail.length === 0 && verified.length === 0 && (
                          <p className="text-gray-600">No item-level detail stored for this run.</p>
                        )}
                        {detail.map((it, i) => (
                          <div key={i} className={`rounded-lg border p-2.5 ${statusColor(it.status)}`}>
                            <div className="flex items-center gap-2">
                              <span className="font-bold">Load {it.load_no || "—"}</span>
                              <span className="text-[10px] uppercase opacity-70">{it.status}</span>
                            </div>
                            {it.reason && <p className="opacity-90 mt-0.5">{it.reason}</p>}
                            {it.email_subject && <p className="opacity-50 mt-0.5 truncate">{it.email_from} — {it.email_subject}</p>}
                          </div>
                        ))}
                        {verified.length > 0 && (
                          <p className="text-green-400/80">✓ Auto-updated {verified.length}: {verified.map((v) => v.load_no).filter(Boolean).join(", ")}</p>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* ── AI PROMPTS (read-only) ───────────────────────────────── */}
        {prompts.length > 0 && (
          <div>
            <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-3">
              AI Prompts{" "}
              <span className="text-gray-600 normal-case font-normal tracking-normal">
                · read-only — edit these in the Demo dashboard
              </span>
            </h2>
            <div className="space-y-2">
              {prompts.map((p) => (
                <details key={p.key} className="bg-[#141414] border border-[#222] rounded-xl p-4">
                  <summary className="cursor-pointer list-none flex items-center justify-between">
                    <span className="font-medium text-gray-200">{p.key}</span>
                    <span className="text-xs text-gray-600">
                      {p.updated_by ? `edited by ${p.updated_by}` : "default"}
                    </span>
                  </summary>
                  {p.description && <p className="text-xs text-gray-500 mt-2">{p.description}</p>}
                  <pre className="mt-2 max-h-72 overflow-auto bg-[#0a0a0a] border border-[#1c1c1c] rounded-lg p-3 text-[11px] leading-5 text-gray-300 whitespace-pre-wrap">
                    {p.content}
                  </pre>
                </details>
              ))}
            </div>
          </div>
        )}
        </div>
      </div>
    </main>
  );
}
