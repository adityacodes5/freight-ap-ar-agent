"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useAuth, API } from "../../lib/useAuth";
import { Nav } from "../Nav";

// A prompt as returned by GET /api/prompts.
interface Prompt {
  key: string;
  content: string;
  description?: string | null;
  updated_at?: string | null;
  updated_by?: string | null;
}

// Friendly, plain-language labels for the prompts we ship. Anything not listed
// here still shows up — it just falls back to a humanised version of its key —
// so a new prompt added later never gets hidden from the editor.
const META: Record<string, { title: string; blurb: string; kind?: "list" }> = {
  carrier_extraction: {
    title: "Carrier invoice reading",
    blurb:
      "How the AI pulls the carrier name, amount, and invoice number off a carrier's bill. Loosen or tighten the rules here if it keeps grabbing the wrong number.",
  },
  shipper_validation: {
    title: "Shipper invoice check",
    blurb:
      "How the AI decides whether one of dispatch's own invoices to a customer is ready to enter, needs a hold, or is unclear.",
  },
  blocked_senders: {
    title: "Blocked senders",
    blurb:
      "Emails from anyone on this list are never read or forwarded. One entry per line — an email address, a domain, or a name. Lines starting with # are notes.",
    kind: "list",
  },
};

function metaFor(p: Prompt) {
  const m = META[p.key];
  if (m) return m;
  const title = p.key
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
  return { title, blurb: p.description || "Controls part of how the AI reads and files invoices." };
}

const timeAgo = (iso?: string | null) => {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
};

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

function PromptCard({
  p,
  onSaved,
  authHeaders,
}: {
  p: Prompt;
  onSaved: (updated: Prompt) => void;
  authHeaders: () => Record<string, string>;
}) {
  const meta = metaFor(p);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(p.content);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Keep the editor in sync if the underlying prompt is refreshed from the server.
  useEffect(() => { if (!open) setDraft(p.content); }, [p.content, open]);

  const dirty = draft !== p.content;
  const lines = p.content.split("\n").length;

  const save = async () => {
    setSaving(true); setErr(null); setSaved(false);
    try {
      const res = await fetch(`${API}/api/prompts`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ key: p.key, content: draft }),
      });
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
      const body = await res.json();
      onSaved({ ...p, content: draft, updated_by: body.updated_by, updated_at: new Date().toISOString() });
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
    } catch (e) {
      setErr(String(e));
    }
    setSaving(false);
  };

  const cancel = () => { setDraft(p.content); setOpen(false); setErr(null); };

  return (
    <div className="bg-[#141414] border border-[#242424] rounded-2xl overflow-hidden">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full text-left p-5 flex items-start justify-between gap-4 hover:bg-[#171717] transition-colors"
      >
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-semibold text-gray-100">{meta.title}</span>
            <span className="text-[10px] font-mono uppercase tracking-wide px-2 py-0.5 rounded-full bg-black/40 text-gray-500">
              {p.key}
            </span>
          </div>
          <p className="text-sm text-gray-500 mt-1">{meta.blurb}</p>
          <p className="text-xs text-gray-600 mt-2">
            {lines} line{lines === 1 ? "" : "s"}
            {p.updated_at && ` · last edited ${timeAgo(p.updated_at)}`}
            {p.updated_by && ` by ${p.updated_by}`}
          </p>
        </div>
        <span className="text-gray-600 text-sm shrink-0 mt-0.5">{open ? "Close" : "Edit"} {open ? "▲" : "▼"}</span>
      </button>

      {open && (
        <div className="px-5 pb-5 border-t border-[#1e1e1e]">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
            rows={Math.min(Math.max(draft.split("\n").length + 1, 8), 30)}
            className="w-full mt-4 bg-[#0d0d0d] border border-[#2a2a2a] focus:border-[#3a3a3a] rounded-xl p-3.5 font-mono text-[13px] leading-relaxed text-gray-200 outline-none resize-y"
          />
          {meta.kind === "list" && (
            <p className="text-xs text-gray-600 mt-2">One entry per line. Lines beginning with # are ignored (use them for notes).</p>
          )}
          {err && <p className="text-red-400 text-sm mt-3">Couldn&apos;t save: {err}</p>}
          <div className="flex items-center gap-3 mt-4">
            <button
              onClick={save}
              disabled={!dirty || saving}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                dirty && !saving
                  ? "bg-blue-600 hover:bg-blue-500 text-white"
                  : "bg-[#1e1e1e] text-gray-600 cursor-not-allowed"
              }`}
            >
              {saving ? "Saving…" : "Save changes"}
            </button>
            <button onClick={cancel} className="text-sm text-gray-500 hover:text-gray-300">
              {dirty ? "Discard" : "Close"}
            </button>
            {saved && <span className="text-sm text-green-400">✓ Saved — live everywhere in ~20s</span>}
            {dirty && !saved && <span className="text-xs text-amber-400/80">unsaved changes</span>}
          </div>
        </div>
      )}
    </div>
  );
}

export default function AgentPage() {
  const { authReady, authorized, email, authHeaders, signIn, signOut, signingIn } = useAuth();
  const [prompts, setPrompts] = useState<Prompt[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true); setErr(null);
    try {
      const res = await fetch(`${API}/api/prompts`, { headers: authHeaders() });
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
      const body = await res.json();
      setPrompts(body.prompts ?? []);
    } catch (e) { setErr(String(e)); }
    setLoading(false);
  }, [authHeaders]);
  useEffect(() => { if (authorized) load(); }, [authorized, load]);

  const onSaved = useCallback((updated: Prompt) => {
    setPrompts((cur) => (cur ? cur.map((p) => (p.key === updated.key ? updated : p)) : cur));
  }, []);

  // Show the meaningful prompts first, in a sensible order, then anything else.
  const ordered = useMemo(() => {
    if (!prompts) return [];
    const rank = (k: string) =>
      ({ carrier_extraction: 0, shipper_validation: 1, blocked_senders: 2 } as Record<string, number>)[k] ?? 9;
    return [...prompts].sort((a, b) => rank(a.key) - rank(b.key) || a.key.localeCompare(b.key));
  }, [prompts]);

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

  return (
    <Shell email={email} onSignOut={signOut}>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">Agent</h1>
          <p className="text-sm text-gray-500">The instructions the AI follows — edit them in plain English, no code.</p>
        </div>
        <button onClick={load} className="text-xs text-gray-500 hover:text-gray-300">↻ refresh</button>
      </div>

      <div className="mb-6 rounded-2xl border border-[#242424] bg-[#111] p-4 text-sm text-gray-400">
        These are the rules the AI reads before it files each invoice. Edit one, hit save, and every part of the
        system — the twice-daily runs and both dashboards — picks up the change within about 20 seconds.
        <span className="text-gray-500"> No deploy, no restart.</span> If something looks wrong, edit the text and it stops immediately.
      </div>

      {err && <p className="text-red-400 text-sm mb-4">Error: {err}</p>}
      {loading && !prompts && <p className="text-gray-500">Loading…</p>}
      {prompts && prompts.length === 0 && !loading && (
        <p className="text-gray-500 text-sm">No editable prompts yet — they appear here the first time a run seeds them.</p>
      )}

      <div className="space-y-3">
        {ordered.map((p) => (
          <PromptCard key={p.key} p={p} onSaved={onSaved} authHeaders={authHeaders} />
        ))}
      </div>
    </Shell>
  );
}
