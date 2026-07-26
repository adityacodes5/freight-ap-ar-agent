"""Run — syncs new sold loads into ACCOUNTS, then checks the last N emails for
both shipper and carrier invoices.

Runs whenever it's invoked — a Render Cron Job on a schedule, or a manual Trigger
Run. There is NO time gate; if you start it, it works. Each run does, in order:
  1. SALES → ACCOUNTS sync — pull newly-sold loads into the Accounts sheet so
     invoices that arrive can match a row.
  2. SHIPPER invoice batch.
  3. CARRIER invoice batch — verified invoices are forwarded to dispatch.

A cross-process lock still prevents two runs (e.g. a manual + scheduled one) from
processing the inbox at the same time.

    python run_scheduled.py                 # full run
    python run_scheduled.py --limit 40      # override emails per lane
    python run_scheduled.py --dry-run       # no writes
"""
import argparse
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

TZ = ZoneInfo("America/Toronto")  # for readable local timestamps in the log only
DEFAULT_LIMIT = 40


def _setup_logging() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    logger.add("logistics_agent.log", rotation="10 MB", retention="30 days", level="DEBUG")


# ── Daily summary email ───────────────────────────────────────────────────────
SUMMARY_TO = os.environ.get("SUMMARY_EMAIL", "owner@demologistics.com")

# Standing CC recipients on the daily digest (owner is the main TO); added Jul 2026.
# Use `or` (not just get's default) so a BLANK SUMMARY_CC env var can't silently
# drop the CC — only a real, non-empty override replaces this list.
DIGEST_CC = (os.environ.get("SUMMARY_CC") or "").strip() or \
    "ap.lead@demologistics.com,ap.clerk@demologistics.com"


def _digest_cc() -> str:
    """Comma-joined CC list for the daily digest, excluding anyone already on the
    TO line so no one is both TO and CC (no duplicate delivery)."""
    to = {p.strip().lower() for p in SUMMARY_TO.split(",") if p.strip()}
    seen: set[str] = set()
    out: list[str] = []
    for addr in [p.strip() for p in DIGEST_CC.split(",")]:
        k = addr.lower()
        if addr and k not in to and k not in seen:
            seen.add(k)
            out.append(addr)
    return ",".join(out)

# Statuses that mean "a REAL load/invoice needs a human" — these are emphasized.
# NOTE: no_load / no_invoice are deliberately NOT here. Those are junk mail the
# agent correctly ignored (NOAs, statements, "copy request" forwards) — they were
# never loads, so they don't belong in the loud box. They go to a quiet footnote.
_ATTN = {"review", "hold", "discrepancy", "no_row", "error"}
_LABEL = {
    "review":      "Review — left unread",
    "hold":        "Payment hold — left unread",
    "discrepancy": "Discrepancy — flagged on sheet",
    "no_row":      "Not in sheet — left unread",
    "error":       "Error",
}

# Statuses that are just noise the agent skipped — counted quietly, never alarmed.
_QUIET = {"no_load", "no_invoice"}


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _age_days(iso, now) -> int | None:
    """Whole days between an ISO timestamp's date and today (local)."""
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return (now.date() - d.date()).days


def _money(d: dict, cur: str) -> str:
    return f"${round(d.get(cur, 0)):,}"


def _ld(x) -> str:
    m = re.search(r"\d+", str(x or ""))
    return m.group() if m else ""


def _build_summary(now, mode: str, sync_msg: str, results: dict, failed: bool,
                   backlog: list | None = None, payables: dict | None = None,
                   resolved_loads: set | None = None, auto_cleared: int = 0):
    """Return (subject, html) for the daily completion email. Skipped / left-unread
    loads are listed first and loud, so they don't get silently forgotten. Also
    resurfaces the standing review backlog (items still open from earlier runs) and
    a carrier-payables outlook, so the one email is a complete 'what needs you'.
    `resolved_loads` are this-run flags already handled on the sheet — dropped from
    the loud box and counted into `auto_cleared` instead."""
    resolved_loads = resolved_loads or set()
    attn, done, quiet = [], [], []
    skipped_resolved = 0
    for lane in ("SHIPPER", "CARRIER"):
        st = results.get(lane) or {}
        if "_error" in st:
            attn.append((lane, {"status": "error", "load_no": "—",
                                "reason": st["_error"], "email_subject": "(lane crashed)"}))
            continue
        for it in st.get("_report_items", []):
            s = it.get("status", "")
            if s in _ATTN:
                if _ld(it.get("load_no")) in resolved_loads:
                    skipped_resolved += 1  # already fixed on the sheet — don't nag
                    continue
                attn.append((lane, it))
            elif s in _QUIET:
                quiet.append((lane, it))
            elif s == "verified":
                done.append((lane, it))
    auto_cleared = (auto_cleared or 0) + skipped_resolved

    # Standing backlog = open review items that predate today (been sitting).
    stale = [it for it in (backlog or []) if (_age_days(it.get("created_at"), now) or 0) >= 1]

    bits = [f"{len(attn)} need you"]
    if stale:
        bits.append(f"{len(stale)} still open")
    bits.append(f"{len(done)} auto-done")
    subj = f"Demo Agent · {now:%b %d} · " + " · ".join(bits)
    if failed:
        subj += " · run errors"

    # ── Clean, monochrome layout. One restrained accent (red) for the only thing
    #    that needs a human. No filled colour boxes, no emoji headers. ────────────
    FONT = ("font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
            "Helvetica,Arial,sans-serif")
    MUTE = "color:#6b7280"
    HEAD = ("font-size:11px;font-weight:600;letter-spacing:.07em;text-transform:"
            "uppercase;color:#9ca3af;margin:26px 0 10px")
    TH = ("text-align:left;padding:6px 8px;font-size:10px;font-weight:600;"
          "text-transform:uppercase;letter-spacing:.04em;color:#9ca3af;"
          "border-bottom:1px solid #e5e7eb")
    TD = "padding:8px;font-size:13px;color:#374151;border-bottom:1px solid #f3f4f6;vertical-align:top"

    h = [f"<div style=\"{FONT};font-size:14px;line-height:1.55;color:#1f2937;"
         "max-width:640px;margin:0 auto;padding:4px 2px\">"]

    # Header — quiet label, date, one-line status.
    h.append("<div style='font-size:11px;font-weight:600;letter-spacing:.09em;"
             "text-transform:uppercase;color:#9ca3af'>Demo Agent · Daily run</div>")
    h.append(f"<div style='font-size:20px;font-weight:600;color:#111827;margin:3px 0 4px'>{now:%A, %b %d}</div>")
    h.append(f"<div style='{MUTE};font-size:13px'>Scanned {_esc(mode)} · " + " · ".join(_esc(b) for b in bits) + "</div>")
    h.append(f"<div style='{MUTE};font-size:12px;margin-top:4px'>Accounts sync: {_esc(sync_msg)}</div>")

    # Needs you — the only emphasized block, a thin red rule, no fill.
    if attn:
        h.append("<div style='margin:22px 0 10px;border-left:3px solid #dc2626;padding:1px 0 1px 12px'>")
        h.append(f"<div style='font-weight:600;color:#b91c1c;font-size:15px'>{len(attn)} need you</div>")
        h.append(f"<div style='{MUTE};font-size:12px;margin-top:2px'>Left unread and not entered — they stay until you handle them.</div>")
        h.append("</div>")
        h.append("<table style='border-collapse:collapse;width:100%'>")
        h.append(f"<tr><th style='{TH}'>Load</th><th style='{TH}'>Lane</th><th style='{TH}'>What</th><th style='{TH}'>Why</th></tr>")
        for lane, it in attn:
            h.append(
                "<tr>"
                f"<td style='{TD};font-weight:600;color:#111827'>{_esc(it.get('load_no', '—'))}</td>"
                f"<td style='{TD};{MUTE}'>{lane.title()}</td>"
                f"<td style='{TD}'>{_LABEL.get(it.get('status'), _esc(it.get('status', '')))}</td>"
                f"<td style='{TD}'>{_esc((it.get('reason') or '')[:130])}"
                f"<div style='{MUTE};font-size:11px;margin-top:2px'>{_esc((it.get('email_subject') or '')[:65])}</div></td>"
                "</tr>")
        h.append("</table>")
    else:
        h.append(f"<div style='margin:22px 0;font-size:14px;color:#15803d'>Nothing needs you — everything scanned was auto-handled.</div>")

    # Auto-updated — compact, one line per lane.
    carrier_done = [it for lane, it in done if lane == "CARRIER"]
    shipper_done = [it for lane, it in done if lane == "SHIPPER"]

    def _done_line(label, items):
        if not items:
            return f"<div style='{MUTE};margin:3px 0'>0 {label}</div>"
        nums = ", ".join(_esc(it.get("load_no", "")) for it in items[:60])
        tail = " …" if len(items) > 60 else ""
        return (f"<div style='margin:3px 0'><b style='color:#111827'>{len(items)} {label}</b>"
                f"<span style='{MUTE};font-size:12px'> — {nums}{tail}</span></div>")

    h.append(f"<div style='{HEAD}'>Auto-updated</div>")
    h.append(_done_line("carrier invoice(s)", carrier_done))
    h.append(_done_line("shipper invoice(s)", shipper_done))
    if auto_cleared:
        h.append(f"<div style='{MUTE};font-size:12px;margin:3px 0'>"
                 f"Auto-cleared {auto_cleared} item(s) already fixed on the sheet.</div>")

    # Still open from earlier runs.
    if stale:
        h.append(f"<div style='{HEAD}'>Still open from earlier</div>")
        h.append("<table style='border-collapse:collapse;width:100%'>")
        h.append(f"<tr><th style='{TH}'>Load</th><th style='{TH}'>Type</th><th style='{TH}'>Waiting</th><th style='{TH}'>Why</th></tr>")
        for it in stale[:20]:
            age = _age_days(it.get("created_at"), now)
            h.append(
                "<tr>"
                f"<td style='{TD};font-weight:600;color:#111827'>{_esc(it.get('load_no', '—'))}</td>"
                f"<td style='{TD};{MUTE}'>{_esc(it.get('status', ''))}</td>"
                f"<td style='{TD};{MUTE}'>{age}d</td>"
                f"<td style='{TD}'>{_esc((it.get('reason') or '')[:120])}</td>"
                "</tr>")
        h.append("</table>")

    # Carrier payables outlook.
    if payables:
        od, ds = payables.get("overdue", {}), payables.get("due_soon", {})
        ndays = payables.get("days", 7)
        h.append(f"<div style='{HEAD}'>Carrier payables</div>")
        h.append(
            f"<div style='color:#374151'>Overdue · {od.get('count', 0)} load(s) · "
            f"{_money(od,'USD')} USD · {_money(od,'CAD')} CAD</div>"
            f"<div style='color:#374151;margin-top:2px'>Due within {ndays} days · {ds.get('count', 0)} load(s) · "
            f"{_money(ds,'USD')} USD · {_money(ds,'CAD')} CAD</div>")
        soon = payables.get("soon_rows") or []
        if soon:
            h.append(f"<div style='{MUTE};font-size:12px;margin-top:6px'>Soonest: "
                     + "; ".join(f"{_esc(r['load'])} ({r['days_until']}d, ${round(r['amount']):,} {r['currency']})"
                                 for r in soon[:6]) + "</div>")
        h.append(f"<div style='{MUTE};font-size:12px;margin-top:4px'>Pay via the ACH/Check sync on the dashboard.</div>")

    # Ignored non-invoice mail — one quiet line.
    if quiet:
        subs = [_esc((it.get("email_subject") or "").strip()[:60]) for _, it in quiet if (it.get("email_subject") or "").strip()]
        line = (f"<div style='{MUTE};font-size:12px;margin-top:22px'>"
                f"Ignored {len(quiet)} non-invoice email(s) — statements, NOAs, copy requests, no load #.")
        if subs:
            line += "<br>" + "; ".join(subs[:12])
        h.append(line + "</div>")

    h.append(f"<div style='border-top:1px solid #e5e7eb;margin-top:24px;padding-top:10px;{MUTE};font-size:11px'>"
             "Sent automatically by the Demo agent after each run.</div></div>")
    return subj, "".join(h)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None,
                    help="ad-hoc: scan the last N emails per lane (default: everything new since the last run)")
    ap.add_argument("--window-days", type=int, default=5,
                    help="watermark lookback floor in days — re-scans this far back for retries / missed runs (default 5)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    _setup_logging()

    now = datetime.now(TZ)

    # Import after env is loaded so DATABASE_URL / USE_DB are picked up.
    from db import acquire_run_lock, release_run_lock
    from run_batch import run_batch
    from run_carrier_batch import run_carrier_batch
    from sync_sales_to_accounts import sync

    # Default = watermark mode: each lane scans everything since ITS OWN last good
    # run (with a lookback floor) and advances its own watermark. --limit forces the
    # fixed "last N" behaviour for ad-hoc use. The runners own the watermark now, so
    # a manual run from the dashboard and this cron share one per-lane timeline.
    use_watermark = args.limit is None
    mode = ("everything since each lane's last run"
            if use_watermark else f"last {args.limit} emails (fixed)")
    logger.info("══ Run @ {} — {}, both lanes ══", now.strftime("%Y-%m-%d %H:%M %Z"), mode)

    if not acquire_run_lock(holder="scheduled"):
        logger.warning("Another run is already in progress — skipping this scheduled run.")
        return 0

    failed = False
    sync_msg = ""
    results: dict = {}
    try:
        # 1. Keep ACCOUNTS current FIRST: pull newly-sold loads from SALES so
        #    invoices arriving today can match a row. Deterministic, no AI.
        logger.info("── ACCOUNTS SYNC (SALES → ACCOUNTS) ──")
        try:
            res = sync(commit=not args.dry_run)
            sync_msg = res["message"]
            logger.info("Accounts sync: {}", sync_msg)
        except Exception as exc:  # noqa: BLE001 — sync failure must not block the email run
            # Best-effort + transient-tolerant. The sync hits Google Sheets and
            # Supabase, which occasionally drop a connection / time out. Retry once
            # before marking the whole run failed, so a one-off hiccup doesn't turn
            # the cron red when the invoice lanes + digest all succeeded. Only a
            # PERSISTENT sync failure (fails twice) flips `failed` → non-zero exit.
            logger.warning("Accounts sync error ({}) — retrying once…", exc)
            try:
                res = sync(commit=not args.dry_run)
                sync_msg = res["message"]
                logger.info("Accounts sync (retry): {}", sync_msg)
            except Exception as exc2:  # noqa: BLE001
                failed = True
                sync_msg = f"FAILED — {exc2}"
                logger.exception("Accounts sync failed after retry: {}", exc2)

        # 2. + 3. Email batches. One lane failing must not block the other. Each
        # runner reads + advances its OWN watermark (per lane), so a lane that
        # crashes here simply doesn't advance and re-scans next time.
        for name, fn in (("SHIPPER", run_batch), ("CARRIER", run_carrier_batch)):
            logger.info("── {} ──", name)
            try:
                results[name] = fn(limit=(args.limit or DEFAULT_LIMIT), dry_run=args.dry_run,
                                   use_watermark=use_watermark, window_days=args.window_days)
            except Exception as exc:  # noqa: BLE001
                failed = True
                results[name] = {"_error": str(exc)}
                logger.exception("{} run failed: {}", name, exc)
    finally:
        release_run_lock()

    # Daily completion email — what got done, (loud) what needs a human, the
    # standing backlog still open from earlier runs, and the payables outlook.
    if not args.dry_run:
        try:
            from config import Config
            from tools.zoho_mail import ZohoMailClient

            backlog, payables = None, None
            resolved_loads, auto_cleared = set(), 0

            # Reconcile FIRST: auto-resolve queue items already handled on the sheet
            # (STATUS set / flag cleared / load now present) so we don't nag about
            # things the team fixed but forgot to mark done. Reuse its Accounts index
            # to also drop any of THIS run's flags that are already fine.
            try:
                from reconcile import reconcile_open_items, resolved_report_loads
                from config import Config as _Cfg
                from tools.sheets import GoogleSheetsClient as _GSC
                _gc = _GSC(_Cfg())
                recon = reconcile_open_items(_gc)
                auto_cleared = recon["cleared_count"]
                report_items = []
                for _lane in ("SHIPPER", "CARRIER"):
                    report_items += (results.get(_lane) or {}).get("_report_items", [])
                resolved_loads = resolved_report_loads(report_items, recon["index"], _gc, recon["bg_cache"])
                logger.info("Reconcile: auto-resolved {} backlog item(s), {} this-run flag(s) already fixed",
                            auto_cleared, len(resolved_loads))
            except Exception as exc:  # noqa: BLE001 — reconciliation is best-effort
                logger.warning("Could not reconcile review queue: {}", exc)

            try:
                from review import list_review
                backlog = list_review()  # AFTER reconcile → only what's still open
            except Exception as exc:  # noqa: BLE001 — best-effort enrichment
                logger.warning("Could not load review backlog for summary: {}", exc)
            try:
                from analytics import payables_snapshot
                payables = payables_snapshot(days=7)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not load payables outlook for summary: {}", exc)

            subj, html = _build_summary(now, mode, sync_msg, results, failed,
                                        backlog=backlog, payables=payables,
                                        resolved_loads=resolved_loads, auto_cleared=auto_cleared)
            digest_cc = _digest_cc()
            if ZohoMailClient(Config()).send_email(SUMMARY_TO, subj, html, cc=digest_cc):
                logger.info("Summary email sent to {} (cc: {})", SUMMARY_TO, digest_cc or "—")
        except Exception as exc:  # noqa: BLE001 — notification must never break the run
            logger.warning("Could not send summary email: {}", exc)

    # Monthly bank-style statement — emailed once, ~7 days after the month ends
    # (gives payments time to settle). Idempotent: a per-month watermark flag stops
    # the two daily runs from sending it twice.
    if not args.dry_run and now.day == 7:
        try:
            from db import get_watermark, set_watermark
            from statement_pdf import _prev_month, send_statement_email
            sy, sm = _prev_month(now.year, now.month)
            flag = f"statement:{sy}-{sm:02d}"
            if get_watermark(flag) == 0:
                if send_statement_email(sy, sm, SUMMARY_TO):
                    set_watermark(int(now.timestamp() * 1000), flag)
                    logger.info("Monthly statement {} emailed to {}", flag, SUMMARY_TO)
        except Exception as exc:  # noqa: BLE001 — statement must never break the run
            logger.warning("Could not send monthly statement: {}", exc)

    logger.info("══ Scheduled run complete ══")

    # All batch work (sheet writes, DB state, dispatch forwards) is finished and
    # committed by this point. On Render, the *normal* interpreter shutdown was
    # crashing a native library (pdfium, via pypdfium2) in the Linux container —
    # AFTER the work was done — so the process got killed (exit code None) and the
    # whole run was marked "failed" even though it succeeded. Flush logs and
    # hard-exit, skipping the finalizers that crash, so the exit code is clean.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    raise SystemExit(main())
