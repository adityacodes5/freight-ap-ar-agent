#!/usr/bin/env python3
"""
One-time completeness audit — make sure no invoice slipped through email.

Read-only. Scans the inbox over a lookback window, and for every email carrying
PDF attachment(s) checks whether the agent actually PROCESSED it (against the same
Supabase processed-state the production runners write to). Flags:

  • MISSED   — email has PDFs, none processed, sender not blocked  → possibly an
               invoice the agent never handled
  • PARTIAL  — some PDFs on the email processed, some not          → e.g. a
               multi-invoice email where one invoice was missed
  • handled  — all PDFs processed (healthy)

It does NOT re-run extraction (no LLM cost); it's a fast cross-check for eyeballing.
Blocked/sensitive senders and no-attachment emails are excluded.

Usage:
    python audit_email.py                 # last 30 days
    python audit_email.py --days 14
    python audit_email.py --days 30 --email        # also send the report
"""
import argparse
import re
from datetime import datetime, timedelta

# Load .env BEFORE importing state/review — they read DATABASE_URL at import time
# to choose DB vs JSON backend. Import them too early and the audit silently reads
# stale local JSON instead of the production Supabase state.
from dotenv import load_dotenv
load_dotenv()

from loguru import logger  # noqa: E402

# Categories the agent legitimately does NOT book as a carrier invoice. Junk
# classification isn't stored durably (only ~20 run reports are kept), so we
# re-derive it heuristically to avoid re-flagging NOAs/void cheques/statements.
_JUNK = ("notice of assignment", "void che", "void check", "debtor statement",
         "remittance", "banking information", "bank details", "statement",
         "pending payment", "payment status", "payment required")
_INVOICE_HINT = ("invoice", "inv", "load")


_NON_INVOICE_PDF = ("pod", "confirmation", "load_conf", "loadconf", "void", "cheque",
                    "check", "deposit", "banking", "bank details", "noa", "backup",
                    "release", "rate con", "ratecon")


def _looks_invoice(name: str) -> bool:
    n = (name or "").lower()
    if any(k in n for k in _NON_INVOICE_PDF):
        return False
    return "invoice" in n or "inv" in n


def _classify(subject: str, pdf_names: list[str], from_addr: str) -> str:
    """carrier | shipper | junk — a coarse pre-filter so the audit only surfaces
    plausibly-real invoice emails the agent has no record of."""
    text = (subject + " " + " ".join(pdf_names)).lower()
    frm = (from_addr or "").lower()
    if "notice of assignment" in text or re.search(r"\bnoa\b", text):
        return "junk"
    if any(k in text for k in _JUNK):
        return "junk"
    if not any(h in text for h in _INVOICE_HINT):
        return "junk"  # nothing suggesting an invoice
    return "shipper" if "dispatch@demologistics.com" in frm else "carrier"

import state
import review
from config import Config
from prompts import is_blocked_sender
from run_carrier_batch import epoch_to_date
from tools.zoho_mail import ZohoMailClient


def _att_id(a: dict) -> str:
    return a.get("attachmentId") or a.get("id", "")


def _seen_message_ids() -> set[str]:
    """Every message_id the agent has already dealt with — whether it processed
    the invoice (agent_processed_invoices) OR looked at it and correctly ignored
    it as junk (NOA / statement / no-load), which lands in the run reports. An
    email in neither was never touched at all."""
    seen: set[str] = set()
    for p in state.list_processed():
        seen.add(p["key"].split("::")[0])
    for rep in review.list_reports():
        for it in (rep.get("items") or []):
            mid = it.get("message_id")
            if mid:
                seen.add(mid)
    return seen


def audit(days: int = 30) -> dict:
    cfg = Config()
    zoho = ZohoMailClient(cfg)
    seen = _seen_message_ids()
    logger.info("Agent has already seen {} distinct email(s)", len(seen))
    since = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
    emails = zoho.list_inbox_since(since)

    scanned = handled = blocked = no_pdf = junk = 0
    carrier, shipper, partial = [], [], []
    for i, e in enumerate(emails):
        if is_blocked_sender(f"{e.get('fromAddress','')} {e.get('sender','')}"):
            blocked += 1
            continue
        mid = e.get("messageId", "")
        try:
            atts = zoho.list_attachments(mid)
        except Exception as exc:  # noqa: BLE001 — one bad email can't stop the audit
            logger.warning("attachment fetch failed for {}: {}", mid, exc)
            continue
        if not atts:
            no_pdf += 1
            continue
        scanned += 1
        pdf_names = [a.get("attachmentName", "") for a in atts]
        done_flags = [state.is_processed(mid, _att_id(a)) for a in atts]
        n_done = sum(done_flags)
        rec = {
            "from": e.get("fromAddress", ""),
            "subject": (e.get("subject", "") or "")[:64],
            "date": epoch_to_date(e.get("receivedTime", "")),
            "pdfs": pdf_names, "n_done": n_done, "n_pdf": len(atts),
        }
        if mid in seen:
            # Only a real concern if an UNPROCESSED PDF itself looks like an invoice
            # (not a POD / load-confirmation / banking doc the agent rightly skips).
            unhandled_invoice = any(
                (not d) and _looks_invoice(nm)
                for d, nm in zip(done_flags, pdf_names))
            if unhandled_invoice:
                partial.append(rec)
            else:
                handled += 1
            continue
        # No record of this email at all — classify what it plausibly is.
        kind = _classify(rec["subject"], pdf_names, rec["from"])
        if kind == "carrier":
            carrier.append(rec)
        elif kind == "shipper":
            shipper.append(rec)
        else:
            junk += 1
        if (i + 1) % 25 == 0:
            logger.info("… scanned {}/{}", i + 1, len(emails))

    return {"window_days": days, "emails_listed": len(emails), "with_pdfs": scanned,
            "handled": handled, "blocked": blocked, "no_pdf": no_pdf, "junk": junk,
            "carrier": carrier, "shipper": shipper, "partial": partial}


def _rows(title, items):
    if not items:
        return
    print(f"\n  {title}")
    for m in items:
        done = f" ({m['n_done']}/{m['n_pdf']} PDFs done)" if m["n_pdf"] > 1 else ""
        print(f"    {m['date']}  {m['from'][:34]:<34}{done}")
        print(f"        subj: {m['subject']}")
        print(f"        pdfs: {', '.join(m['pdfs'])}")


def _print(r: dict) -> None:
    print("\n" + "=" * 72)
    print(f"  EMAIL COMPLETENESS AUDIT — last {r['window_days']} days")
    print("=" * 72)
    print(f"  Emails listed        {r['emails_listed']}")
    print(f"  With PDF attachments {r['with_pdfs']}")
    print(f"    ✓ handled          {r['handled']}")
    print(f"    ⛔ carrier invoice, no record   {len(r['carrier'])}")
    print(f"    ❓ shipper invoice, no record   {len(r['shipper'])}")
    print(f"    ⚠ multi-invoice, verify        {len(r['partial'])}")
    print(f"  Suppressed: {r['junk']} junk (NOA/void/statement), "
          f"{r['blocked']} blocked sender, {r['no_pdf']} no-attachment")

    _rows("⛔ CARRIER invoice emails with NO processing record — check these:", r["carrier"])
    _rows("❓ SHIPPER invoice emails (from dispatch) with no record:", r["shipper"])
    _rows("⚠ MULTI-INVOICE emails — a PDF may have been missed (verify):", r["partial"])
    if not (r["carrier"] or r["shipper"] or r["partial"]):
        print("\n  ✅ Nothing missed — every real invoice email is accounted for.")
    print("\n  Note: 'no record' can also mean the email arrived after the last agent run.")
    print()


def _html(r: dict) -> str:
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    rows = ""
    for tag, items in (("⛔ Carrier, no record", r["carrier"]),
                       ("❓ Shipper, no record", r["shipper"]),
                       ("⚠ Multi-invoice verify", r["partial"])):
        for m in items:
            rows += (f"<tr><td>{tag}</td><td>{esc(m['date'])}</td><td>{esc(m['from'])}</td>"
                     f"<td>{esc(m['subject'])}</td><td>{m['n_done']}/{m['n_pdf']}</td>"
                     f"<td>{esc(', '.join(m['pdfs']))}</td></tr>")
    if not rows:
        rows = "<tr><td colspan='6'>✅ Nothing missed — every real invoice email is accounted for.</td></tr>"
    return (f"<h2>Email completeness audit — last {r['window_days']} days</h2>"
            f"<p>{r['with_pdfs']} PDF emails: {r['handled']} handled, "
            f"{len(r['carrier'])} carrier + {len(r['shipper'])} shipper with no record, "
            f"{len(r['partial'])} to verify. ({r['junk']} junk suppressed.)</p>"
            f"<table border='1' cellpadding='6' cellspacing='0'>"
            f"<tr><th>Status</th><th>Date</th><th>From</th><th>Subject</th><th>Done</th><th>PDFs</th></tr>"
            f"{rows}</table>")


def main() -> None:
    ap = argparse.ArgumentParser(description="One-time email completeness audit.")
    ap.add_argument("--days", type=int, default=30, help="lookback window (default 30)")
    ap.add_argument("--email", action="store_true", help="also email the report to SUMMARY_EMAIL")
    args = ap.parse_args()

    r = audit(days=args.days)
    _print(r)

    if args.email:
        import os
        to = os.environ.get("SUMMARY_EMAIL", "")
        if to:
            ZohoMailClient(Config()).send_email(
                to, f"Email completeness audit — last {args.days} days", _html(r))
            print(f"  Report emailed to {to}")


if __name__ == "__main__":
    main()
