"""
Batch processor — runs over the last N inbox emails and processes
every dispatch invoice it finds.

Rules:
  - Only processes emails from dispatch@demologistics.com
  - Only emails with subject matching Invoice_XXXX Load_XXXX
  - Skips if already processed (local state file)
  - Skips if no matching Load no row in the Accounts sheet → leaves email UNREAD
  - If matching row found → updates sheet, marks email READ, bolds Invoice no
  - If data discrepancy → writes note in Remarks + highlights row RED
"""

import json
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("America/Toronto")

import anthropic
from loguru import logger

from config import Config
from tools.zoho_mail import ZohoMailClient
from tools.sheets import GoogleSheetsClient
from tools.parser import extract_text_from_pdf, iter_pdf_pages_base64
import state as invoice_state
from review import RunReport
from prompts import get_prompt, is_blocked_sender
from db import get_watermark, set_watermark

# Each lane keeps its OWN watermark so a manual single-lane run advances only that
# lane and can't make the other lane skip emails.
LANE = "shipper"


DISPATCH_SENDER = "dispatch@demologistics.com"

# Default shipper-validation prompt. The live text is editable in the
# `agent_prompts` table (key "shipper_validation"); this is the fallback used
# when the DB is unreachable or the key hasn't been seeded. Keep them in sync.
DEFAULT_SHIPPER_PROMPT = """You are validating a freight SHIPPER invoice — billed by Demo's dispatch to a
customer — before it is entered into accounting. You are given one or more attached
DOCUMENTS and the EMAIL THREAD.

Hard rules:
- A valid entry REQUIRES an INVOICE for the load. A rate confirmation is NOT
  required and NOT expected — dispatch (shipper) invoices do not need one.
- If there is no actual invoice document (e.g. a payment-status chaser, a rate
  confirmation on its own with no invoice, or a text-only email) -> decision "unsure".
- Read the EMAIL THREAD for context. If dispatch and the sender/invoicer are
  arguing or disputing the rate/charges, or anyone asks to hold/stop payment ->
  decision "dispute_hold".
- If anything is genuinely ambiguous -> decision "unsure".
- If an INVOICE is present and there is no dispute -> decision "proceed".

Return ONLY valid JSON:
{
  "has_invoice": false,
  "load_number": "...",
  "customer_name": "...",
  "shipper_amount": 0.00,
  "hst_amount": 0.00,
  "currency": "CAD or USD",
  "invoice_date": "YYYY-MM-DD or null",
  "decision": "proceed | dispute_hold | unsure",
  "reason": "one short sentence"
}"""

# Matches: Invoice_5633, Invoice 5633, Re: Invoice_5633 Load_5633, etc.
SUBJECT_RE = re.compile(r"Invoice[_\s#-]*(\d{4,})[_\s]*(?:Load[_\s#]*(\d{4,}))?", re.IGNORECASE)

# Dispatch also forwards "Invoice and POD <load>" emails (proof-of-delivery is in,
# invoice ready to clear). These don't fit SUBJECT_RE because of the words between
# "Invoice" and the number, so match them separately: a POD/Invoice subject + a
# 4+ digit load number anywhere in it.
POD_SUBJECT_RE = re.compile(r"\bP\.?\s*O\.?\s*D\b", re.IGNORECASE)


def _subject_load(subject: str) -> str:
    """Return the load number if this subject is a processable dispatch invoice/POD
    email, else "". Handles both 'Invoice_5633 Load_5633' and 'Invoice and POD 5715'."""
    subject = str(subject or "")
    m = SUBJECT_RE.search(subject)
    if m:
        return m.group(2) or m.group(1)
    # POD forward: needs both an invoice/POD cue and a 4+ digit number
    if POD_SUBJECT_RE.search(subject) and re.search(r"invoice", subject, re.IGNORECASE):
        nums = re.findall(r"\d{4,}", subject)
        if nums:
            return nums[-1]
    return ""


def _from_dispatch(email: dict) -> bool:
    """True only if the email was SENT BY dispatch@ — i.e. a real shipper invoice
    (dispatch billing a customer). Being merely CC'd or To dispatch does NOT count:
    a carrier emailing their own invoice and CC-ing dispatch is a CARRIER invoice,
    not a shipper one, and must not land in the shipper list."""
    frm = " ".join([
        email.get("fromAddress", ""),
        email.get("sender", ""),
    ]).lower()
    return DISPATCH_SENDER in frm


def parse_amount(val: str) -> float:
    try:
        return float(str(val).replace(",", "").replace("$", "").strip())
    except (ValueError, AttributeError):
        return 0.0


def fmt_date(iso_date: str) -> str:
    """Convert YYYY-MM-DD → MM/DD/YYYY."""
    try:
        return datetime.strptime(iso_date[:10], "%Y-%m-%d").strftime("%m/%d/%Y")
    except ValueError:
        return iso_date


def epoch_to_date(ts: str) -> str:
    """Convert Zoho epoch-ms string → MM/DD/YYYY in Toronto local time. Local, NOT
    UTC — an email received in the evening EDT is the NEXT day in UTC, which was
    writing the wrong 'IN received' date (off-by-one)."""
    try:
        return datetime.fromtimestamp(int(str(ts)[:10]), _TZ).strftime("%m/%d/%Y")
    except (ValueError, OSError):
        return ""


def _earliest_date(dates) -> str:
    """Return the chronologically earliest MM/DD/YYYY string from an iterable,
    ignoring blanks/unparseable values. 'IN received' should reflect the ORIGINAL
    arrival of the load's invoice, not a later re-send/forward/reply."""
    best_dt, best_s = None, ""
    for d in dates:
        s = str(d or "").strip()
        if not s:
            continue
        try:
            dt = datetime.strptime(s, "%m/%d/%Y")
        except ValueError:
            continue
        if best_dt is None or dt < best_dt:
            best_dt, best_s = dt, s
    return best_s


def run_batch(limit: int = 500, dry_run: bool = False, progress_callback=None,
              since_ts: int = 0, use_watermark: bool = False, window_days: int = 5) -> dict:
    """Run the shipper batch. Returns stats dict. Optional progress_callback(str).

    Scan window:
      • use_watermark=True → scan everything since this lane's saved watermark
        (with a `window_days` floor so a long outage can't force a giant scan), and
        ADVANCE the watermark on a clean, non-dry run. This is the default for the
        cron and for manual runs with no explicit count.
      • since_ts>0 → scan everything newer than that timestamp (explicit override).
      • otherwise → scan the last `limit` emails (fixed mode; no watermark touched).
    """
    sink_id = None
    if progress_callback:
        sink_id = logger.add(
            lambda msg: progress_callback(msg.record["message"]),
            format="{message}",
            level="INFO",
        )

    config = Config()
    zoho   = ZohoMailClient(config)
    sheets = GoogleSheetsClient(config)
    ai     = anthropic.Anthropic(api_key=config.anthropic_api_key)

    # Resolve the scan window. In watermark mode, start where we last finished but
    # never earlier than the floor (so the window shrinks to just-new emails once
    # we've caught up — the whole point of the watermark).
    run_start_ms = int(time.time() * 1000)
    if use_watermark:
        floor = run_start_ms - window_days * 86_400_000
        wm = get_watermark(LANE)
        since_ts = max(wm, floor) if wm else floor

    dry = " [DRY-RUN]" if dry_run else ""
    logger.info("── SHIPPER INVOICES{} ──────────────────────────────", dry)
    report = RunReport("shipper")

    headers = sheets.get_headers(config.accounts_sheet_name)
    records = sheets.get_all_records(config.accounts_sheet_name)
    for i, r in enumerate(records):
        r["__row_number__"] = i + 2

    load_index: dict[str, dict] = {}
    for r in records:
        ln = str(r.get("Load no", "")).strip()
        if ln:
            load_index[ln] = r

    emails = zoho.list_inbox_since(since_ts) if since_ts else zoho.list_inbox(limit=limit)
    if since_ts:
        logger.info("Scanning everything since {}", datetime.fromtimestamp(since_ts / 1000).strftime("%Y-%m-%d %H:%M"))

    dispatch_emails = []
    for e in emails:
        subject = str(e.get("subject", ""))
        # Sensitive senders (e.g. Example Vendor A, Example Vendor B) — never process.
        if is_blocked_sender(f"{e.get('fromAddress','')} {e.get('sender','')}"):
            continue
        # Only emails actually SENT BY dispatch (the shipper-invoice sender). An
        # email merely CC'd to dispatch (e.g. a carrier's own invoice) is NOT a
        # shipper invoice — it belongs to the carrier lane.
        if _from_dispatch(e) and _subject_load(subject):
            dispatch_emails.append(e)

    logger.info("Scanning {} dispatch/chain emails of {} fetched…", len(dispatch_emails), len(emails))

    # ── Counters ──────────────────────────────────────────────────────────────
    stats = {
        "verified": 0,
        "discrepancy": 0,
        "skipped_no_match": 0,
        "skipped_already_done": 0,
        "hold": 0,
        "review": 0,
        "errors": 0,
    }

    # ── Process each email ────────────────────────────────────────────────────
    for email in dispatch_emails:
        msg_id  = email["messageId"]
        subject = email.get("subject", "")
        recv_ts = email.get("receivedTime", "")
        recv_date = epoch_to_date(recv_ts)

        from_addr = email.get("fromAddress", "")
        load_no = _subject_load(subject)

        # ── Skip if no match in sheet ─────────────────────────────────────
        if load_no not in load_index:
            logger.info("  Load {:>5}  →  no sheet row, left unread", load_no)
            report.add(load_no, "no_row", "Load not yet in Accounts sheet",
                       email_from=from_addr, email_subject=subject, message_id=msg_id)
            stats["skipped_no_match"] += 1
            continue

        row_data = load_index[load_no]
        row_num  = row_data["__row_number__"]

        # ── Skip if Invoice no already filled (already processed) ─────────
        existing_inv = str(row_data.get("Invoice no", "")).strip()
        if existing_inv:
            stats["skipped_already_done"] += 1
            if not dry_run:
                zoho.mark_as_read(msg_id)
            continue

        # ── Gather the load's documents (this email + its chain) ──────────────
        # We need an INVOICE for the load (dispatch/shipper invoices do NOT need a
        # rate confirmation). Documents may live in different emails of the same
        # load's chain, so we collect across the chain; the AI then judges the
        # chain context (a dispute over the rate is a hold). We never enter
        # anything from a text-only/chaser email with no documents.
        chain = [(msg_id, recv_date)]
        for other in emails:
            if other["messageId"] == msg_id:
                continue
            if _subject_load(str(other.get("subject", ""))) != load_no:
                continue
            if str(other.get("hasAttachment", "0")) in ("1", "true", "True"):
                chain.append((other["messageId"], epoch_to_date(other.get("receivedTime", ""))))

        # 'IN received' = the ORIGINAL arrival of the invoice, so use the earliest
        # date across the load's whole email chain — a later re-send, forward, or
        # reply must not push the date forward.
        recv_date = _earliest_date([rd for _, rd in chain]) or recv_date

        docs = []          # [{name, text, images}]
        seen_names = set()
        primary_att_id = ""
        for src_mid, _rd in chain:
            try:
                src_atts = zoho.list_attachments(src_mid)
            except Exception:
                continue
            for a in src_atts:
                name = str(a.get("attachmentName", ""))
                if name in seen_names:
                    continue
                seen_names.add(name)
                aid = a.get("attachmentId") or a.get("id", "")
                if not primary_att_id:
                    primary_att_id = aid
                try:
                    pb = zoho.download_attachment(src_mid, aid)
                    txt = extract_text_from_pdf(pb)
                except Exception as exc:
                    logger.info("  Load {:>5}     could not read {} ({})", load_no, name, exc)
                    continue
                imgs = []
                if not txt.strip():            # scanned → read with vision
                    try:
                        imgs = list(iter_pdf_pages_base64(pb, max_pages=4))
                    except Exception:
                        imgs = []
                del pb
                if txt.strip() or imgs:
                    docs.append({"name": name, "text": txt, "images": imgs})
                if len(docs) >= 6:
                    break
            if len(docs) >= 6:
                break

        # idempotency — keyed on the triggering email + first document
        if primary_att_id and invoice_state.is_processed(msg_id, primary_att_id):
            stats["skipped_already_done"] += 1
            continue
        att_id = primary_att_id

        if not docs:
            # No readable document anywhere → payment-status chaser or text-only
            # email. Never enter from this; flag on the dashboard, leave unread.
            logger.info("  Load {:>5}  ⚑  no invoice/rate-con attached — manual review", load_no)
            report.add(load_no, "review",
                       "No invoice/rate-confirmation document attached (possible payment-status chaser)",
                       sheet_row=row_num, email_from=from_addr, email_subject=subject, message_id=msg_id)
            stats["review"] += 1
            continue

        # ── One AI pass: require an invoice (no rate-con needed), judge chain ──
        try:
            chain_text = zoho.get_message_text(email["messageId"])
        except Exception:
            chain_text = ""

        prompt = get_prompt("shipper_validation", DEFAULT_SHIPPER_PROMPT)

        content = []
        for i, d in enumerate(docs, 1):
            content.append({"type": "text", "text": f"=== DOCUMENT {i}: {d['name']} ==="})
            if d["text"].strip():
                content.append({"type": "text", "text": d["text"][:5000]})
            for img in d["images"]:
                content.append({"type": "image",
                                "source": {"type": "base64", "media_type": "image/jpeg", "data": img}})
        content.append({"type": "text", "text": f"=== EMAIL THREAD ===\n{chain_text[:4000]}"})
        content.append({"type": "text", "text": prompt})

        try:
            resp = ai.messages.create(model="claude-sonnet-5", max_tokens=700,
                                      thinking={"type": "disabled"},
                                      messages=[{"role": "user", "content": content}])
            raw = "".join(getattr(b, "text", "") or "" for b in resp.content
                          if getattr(b, "type", "text") != "thinking").strip()
            if raw.startswith("```"):
                raw = "\n".join(l for l in raw.split("\n") if not l.startswith("```")).strip()
            decision = json.loads(raw)
            if isinstance(decision, list):
                decision = next((x for x in decision if isinstance(x, dict)), {})
        except Exception as exc:
            logger.info("  Load {:>5}  ⚑  could not read documents ({}) — manual review", load_no, exc)
            report.add(load_no, "review", f"Document read failed: {exc}",
                       sheet_row=row_num, email_from=from_addr, email_subject=subject, message_id=msg_id)
            stats["review"] += 1
            continue

        verdict = str(decision.get("decision") or "unsure").lower()

        # Anything that isn't a clean "proceed" → dashboard review, leave the email
        # UNREAD, and do NOT touch the sheet (per the manual-review rule).
        if verdict != "proceed":
            reason = str(decision.get("reason") or verdict)
            label = {
                "dispute_hold": "Dispute/hold in email thread",
                "unsure": "Unclear — needs a human",
            }.get(verdict, "Needs manual review")
            status = "hold" if verdict == "dispute_hold" else "review"
            logger.info("  Load {:>5}  ⚑  {} — left UNREAD for review: {}", load_no, label, reason)
            report.add(load_no, status, f"{label}: {reason}",
                       sheet_row=row_num, email_from=from_addr, email_subject=subject, message_id=msg_id)
            stats[status] += 1
            continue

        invoice = decision  # clean proceed — use the extracted fields below

        # ── Compare sheet vs invoice ──────────────────────────────────────
        sheet_cust = str(row_data.get("Customer Name", "")).strip().lower()
        inv_cust   = str(invoice.get("customer_name", "")).strip().lower()
        name_ok    = bool(sheet_cust and inv_cust and (
            sheet_cust in inv_cust or inv_cust in sheet_cust
        ))

        sheet_amt = parse_amount(row_data.get("Shipper Amt", ""))
        inv_amt   = float(invoice.get("shipper_amount") or 0)
        amt_ok    = sheet_amt == 0 or inv_amt == 0 or abs(sheet_amt - inv_amt) <= 1.0
        all_ok    = name_ok and amt_ok

        # ── Build updates dict ────────────────────────────────────────────
        updates: dict = {}

        updates["Invoice no"] = load_no  # always copy Load no → Invoice no

        raw_inv_date = invoice.get("invoice_date") or ""
        if raw_inv_date:
            updates["invoice date"] = fmt_date(raw_inv_date)

        # Only set 'IN received' if it's still blank — never overwrite. The carrier
        # batch may already have recorded an EARLIER arrival (e.g. the carrier
        # invoice landed days before this shipper invoice); clobbering it pushed the
        # date forward. Whichever lane sees the load's first invoice wins.
        if recv_date and not str(row_data.get("IN received", "")).strip():
            updates["IN received"] = recv_date
            # NOTE: do NOT write "Payment due date" — that column is a Google Sheets
            # formula (=IN received + 30). Writing a value here clobbers the formula.

        hst = float(invoice.get("hst_amount") or 0)
        if hst > 0 and not str(row_data.get("HST", "")).strip():
            updates["HST"] = hst

        customer = invoice.get("customer_name") or row_data.get("Customer Name", "")
        details = {
            "customer_invoice": invoice.get("customer_name", ""),
            "customer_sheet": row_data.get("Customer Name", ""),
            "amount_invoice": inv_amt, "amount_sheet": sheet_amt,
            "invoice_number": load_no,
        }
        if all_ok:
            updates["STATUS"]  = "Verified"
            updates["Remarks"] = "SHIPPER — Invoice verified ✓"
            logger.info("  Load {:>5}  ✓  Shipper verified — {}", load_no, customer)
            stats["verified"] += 1
            final_status, final_reason = "verified", ""
        else:
            notes = []
            if not name_ok:
                notes.append(f"Name: '{invoice.get('customer_name','')}' vs '{row_data.get('Customer Name','')}'")
            if not amt_ok and inv_amt > 0:
                notes.append(f"Amount: invoice=${inv_amt} vs sheet=${sheet_amt}")
            # Prefix so a human instantly knows this is the SHIPPER side (the carrier
            # batch writes to the same Remarks column, previously indistinguishable).
            updates["Remarks"] = "SHIPPER — " + " | ".join(notes)
            logger.info("  Load {:>5}  ⚠  Shipper DISCREPANCY — {}", load_no, updates["Remarks"])
            stats["discrepancy"] += 1
            final_status, final_reason = "discrepancy", updates["Remarks"]

        if dry_run:
            logger.info("  Load {:>5}  [DRY-RUN] would write {}", load_no, list(updates.keys()))
            report.add(load_no, final_status, final_reason, sheet_row=row_num,
                       email_from=from_addr, email_subject=subject,
                       message_id=msg_id, details=details)
            continue

        # ── Stage 1: write to sheet ───────────────────────────────────────
        try:
            sheets.update_cells_by_header(config.accounts_sheet_name, row_num, updates)
            sheets.bold_cell_by_header(config.accounts_sheet_name, row_num, "Invoice no")
            if not all_ok:
                sheets.highlight_row_magenta(config.accounts_sheet_name, row_num)
        except Exception as exc:
            logger.info("  Load {:>5}  ✗  sheet write failed (will retry): {}", load_no, exc)
            report.add(load_no, "error", f"Sheet write failed: {exc}", sheet_row=row_num,
                       email_from=from_addr, email_subject=subject, message_id=msg_id)
            stats["errors"] += 1
            continue

        # ── Stage 2: sheet succeeded → mark read + record state ───────────
        zoho.mark_as_read(msg_id)
        invoice_state.mark_processed(
            msg_id, att_id,
            invoice_number=str(invoice.get("invoice_number", load_no)),
            carrier_name=str(invoice.get("customer_name", "")),
            sheet_row=row_num,
        )
        logger.info("  Load {:>5}  ✓  sheet updated, email marked read", load_no)
        report.add(load_no, final_status, final_reason, sheet_row=row_num,
                   email_from=from_addr, email_subject=subject,
                   message_id=msg_id, details=details)

    # ── Summary ───────────────────────────────────────────────────────────────
    logger.info("─" * 52)
    logger.info(
        "  ✓ {}  ⚠ {}  ⛔ {} hold  ⚑ {} review  → {} no row  → {} already done  ✗ {} errors",
        stats["verified"], stats["discrepancy"], stats["hold"], stats["review"],
        stats["skipped_no_match"], stats["skipped_already_done"], stats["errors"],
    )

    if not dry_run:
        report.finish()

    # Advance this lane's watermark so the NEXT run starts where this one finished.
    # Use run_start (not "now") so an email that arrived mid-run is still caught
    # next time. Only on a real, non-dry watermark run.
    if use_watermark and not dry_run:
        set_watermark(run_start_ms, LANE)
        logger.info("Watermark ({}) advanced — next run starts here.", LANE)

    if sink_id is not None:
        logger.remove(sink_id)

    stats["_report_items"] = report.items  # for the audit harness (per-load detail)
    return stats


if __name__ == "__main__":
    import argparse
    from loguru import logger as _log
    _log.remove()
    _log.add(sys.stderr, level="INFO",
             format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    _log.add("logistics_agent.log", rotation="10 MB", retention="30 days", level="DEBUG")

    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    run_batch(limit=args.limit, dry_run=args.dry_run)
