"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useAuth, API } from "../lib/useAuth";
import { Nav } from "./Nav";

interface ReviewLite { load_no: string; type: string; status: string; reason: string; key: string }
interface Flows { count: number; CAD: number; USD: number }
interface Today {
  review: { count: number; items: ReviewLite[] };
  payables: { overdue?: Flows; due_soon?: Flows };
  receivables: { count?: number; totals?: Record<string, number> };
  last_run: { type: string; finished_at: string; counts: Record<string, number> } | null;
  run_health?: RunHealth;
  as_of: string;
}
interface RunHealth {
  status: "ok" | "errors" | "stale";
  errors: number;
  last_finished: string | null;
  next_run: string;
  schedule_label: string;
}

const money = (n: number) => `$${Math.round(n || 0).toLocaleString()}`;
const when = (iso?: string | null) =>
  iso ? new Date(iso).toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" }) : "";

/** A one-line read on whether the scheduled agent is healthy — so a quiet
 *  dashboard ("all clear") is never confused with a dead cron. */
function RunHealthStrip({ h }: { h: RunHealth }) {
  const style = {
    ok: "border-[#242424] bg-[#111] text-gray-400",
    errors: "border-amber-700/50 bg-amber-950/20 text-amber-200",
    stale: "border-red-800/60 bg-red-950/25 text-red-200",
  }[h.status];
  const icon = { ok: "✓", errors: "⚠", stale: "⛔" }[h.status];
  const msg =
    h.status === "stale"
      ? `Agent hasn't run since ${h.last_finished ? when(h.last_finished) : "— no run on record"}. It may be down.`
      : h.status === "errors"
        ? `Last run finished with ${h.errors} error${h.errors === 1 ? "" : "s"} — worth a look in Runs.`
        : `Agent ran ${when(h.last_finished)}. Everything's on schedule.`;
  return (
    <Link href="/runs" className={`block mb-6 rounded-2xl border ${style} px-4 py-3 text-sm hover:opacity-90 transition-opacity`}>
      <span className="font-medium">{icon} {msg}</span>
      <span className="text-gray-500"> · next run {when(h.next_run)} ({h.schedule_label})</span>
    </Link>
  );
}

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

/** One action row: a headline number, a note, and where it takes you. */
function ActionCard({ href, tone, title, value, note, cta }: {
  href: string; tone: "red" | "amber" | "sky" | "green" | "gray";
  title: string; value: string; note?: string; cta: string;
}) {
  const ring = {
    red: "border-red-800/50 hover:border-red-600",
    amber: "border-amber-700/50 hover:border-amber-500",
    sky: "border-sky-800/50 hover:border-sky-600",
    green: "border-green-800/50 hover:border-green-600",
    gray: "border-[#2a2a2a] hover:border-[#3a3a3a]",
  }[tone];
  const valueColor = {
    red: "text-red-400", amber: "text-amber-300", sky: "text-sky-400", green: "text-green-400", gray: "text-gray-300",
  }[tone];
  return (
    <Link href={href}
      className={`block bg-[#141414] border ${ring} rounded-2xl p-5 transition-all group`}>
      <div className="flex items-start justify-between">
        <div className="text-xs text-gray-500 uppercase tracking-wider">{title}</div>
        <span className="text-gray-600 group-hover:text-gray-400 text-sm">{cta} →</span>
      </div>
      <div className={`text-3xl font-bold mt-1 ${valueColor}`}>{value}</div>
      {note && <div className="text-xs text-gray-500 mt-1">{note}</div>}
    </Link>
  );
}

export default function TodayPage() {
  const { authReady, authorized, email, authHeaders, signIn, signOut, signingIn } = useAuth();
  const [data, setData] = useState<Today | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true); setErr(null);
    try {
      const res = await fetch(`${API}/api/today`, { headers: authHeaders() });
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
          <p className="text-gray-400 mb-4">{email ? `${email} is not authorized.` : "Sign in to continue."}</p>
          <button onClick={signIn} disabled={signingIn}
            className="px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-medium">
            {signingIn ? "Redirecting…" : "Sign in with Google"}
          </button>
        </div>
      </Shell>
    );

  const rev = data?.review;
  const od = data?.payables?.overdue;
  const ds = data?.payables?.due_soon;
  const ar = data?.receivables;
  const lr = data?.last_run;
  const allClear = data && (rev?.count ?? 0) === 0 && (ds?.count ?? 0) === 0;

  return (
    <Shell email={email} onSignOut={signOut}>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">Today</h1>
          <p className="text-sm text-gray-500">What needs you, at a glance · {data?.as_of}</p>
        </div>
        <button onClick={load} className="text-xs text-gray-500 hover:text-gray-300">↻ refresh</button>
      </div>

      {err && <p className="text-red-400 text-sm mb-4">Error: {err}</p>}
      {loading && !data && <p className="text-gray-500">Loading…</p>}

      {data && (
        <>
          {data.run_health && <RunHealthStrip h={data.run_health} />}
          {allClear && (
            <div className="mb-6 rounded-2xl border border-green-800/40 bg-green-950/20 p-5 text-green-300">
              ✓ All clear — nothing needs you and no carrier payments due this week.
            </div>
          )}

          <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4 mb-8">
            <ActionCard href="/runs" tone={(rev?.count ?? 0) > 0 ? "red" : "green"}
              title="Needs review" value={String(rev?.count ?? 0)}
              note={(rev?.count ?? 0) > 0 ? "flags waiting on the sheet" : "queue is clear"} cta="Review" />
            <ActionCard href="/payables" tone={(ds?.count ?? 0) > 0 ? "amber" : "gray"}
              title="Payables due this week" value={String(ds?.count ?? 0)}
              note={ds ? `${money(ds.USD)} USD · ${money(ds.CAD)} CAD` : ""} cta="Pay run" />
            <ActionCard href="/payables" tone={(od?.count ?? 0) > 0 ? "red" : "gray"}
              title="Payables overdue" value={String(od?.count ?? 0)}
              note={od ? `${money(od.USD)} USD · ${money(od.CAD)} CAD` : ""} cta="View" />
            <ActionCard href="/overdue" tone={(ar?.count ?? 0) > 0 ? "sky" : "gray"}
              title="Overdue receivables" value={String(ar?.count ?? 0)}
              note={ar?.totals ? `${money(ar.totals.USD ?? 0)} USD · ${money(ar.totals.CAD ?? 0)} CAD owed to us` : ""} cta="View" />
            <ActionCard href="/receivables" tone="gray"
              title="Record a payment" value="Cheque" note="split a cheque across loads" cta="Open" />
            <ActionCard href="/analytics" tone="gray"
              title="Analytics" value="Cash flow" note="margins, customers, statements" cta="Open" />
          </div>

          {/* Top review items inline */}
          {rev && rev.count > 0 && (
            <div className="mb-8">
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400">Top items to review</h2>
                <Link href="/runs" className="text-xs text-gray-500 hover:text-gray-300">see all →</Link>
              </div>
              <div className="space-y-2">
                {rev.items.map((it) => (
                  <div key={it.key} className="rounded-xl border border-[#242424] bg-[#141414] p-3.5 flex items-start gap-3">
                    <span className="text-[10px] uppercase tracking-wide px-2 py-0.5 rounded-full bg-black/40 text-gray-400 shrink-0 mt-0.5">{it.status}</span>
                    <div className="min-w-0">
                      <span className="font-semibold text-gray-200">Load {it.load_no}</span>
                      <span className="text-gray-600 text-xs ml-2">{it.type}</span>
                      <p className="text-sm text-gray-400 truncate">{it.reason}</p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {lr && (
            <div className="text-xs text-gray-600">
              Last run · {lr.type} · {new Date(lr.finished_at).toLocaleString()} ·
              <span className="text-green-500"> ✓ {lr.counts.verified ?? 0}</span>
              <span className="text-red-500"> ⚠ {lr.counts.discrepancy ?? 0}</span>
              {(lr.counts.hold ?? 0) > 0 && <span className="text-amber-400"> ⛔ {lr.counts.hold}</span>}
              <Link href="/runs" className="text-gray-500 hover:text-gray-300 ml-1">· open Runs</Link>
            </div>
          )}
        </>
      )}
    </Shell>
  );
}
