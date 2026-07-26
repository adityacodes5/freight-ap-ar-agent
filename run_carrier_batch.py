"""
Carrier Invoice Batch Processor

Scans the inbox for carrier invoices (from trucking companies and factoring
companies), matches them to the Accounts sheet by load number (col A), and
fills in carrier-side columns.

Columns updated:
  Col 12  Carrier In#     — carrier's own invoice number
  Col 13  Factoring       — yes / no
  Col 14  IN received     — date of email (MM/DD/YYYY)
  Col 16  Carrier Amount  — verify against invoice (flag if mismatch)
  Col 17  Currency        — CAD or USD from invoice
  Col 24  Factoring       — factoring company name + location
  Col 25  Remarks         — discrepancy notes
  Col 20  STATUS          — Verified / Discrepancy

Rules:
  - Skip emails from dispatch@demologistics.com (those are shipper invoices)
  - Skip emails with no PDF attachment
  - Skip emails with no invoice-related keywords in subject
  - Skip if no load number found in the PDF → leave email unread
  - Skip if load already has Carrier In# filled → leave email unread (already done)
  - Carrier name fuzzy-match against col K — flag if mismatch
  - Amount mismatch or name mismatch → highlight row RED + note
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

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
LANE = "carrier"


DISPATCH_SENDER  = "dispatch@demologistics.com"
FORWARD_TO       = os.environ.get("FORWARD_TO", "dispatch@demologistics.com")
SMTP_PASSWORD    = os.environ.get("ZOHO_SMTP_PASSWORD", "")


def _is_own_company(name: str) -> bool:
    """True if a 'carrier name' is actually US (Demo Logistics). A carrier
    invoice bills Demo for a third-party carrier's haul — it never names Demo
    as the carrier. When extraction comes back with Demo as the carrier, the
    document is really our OWN shipper invoice (dispatch → customer, "Hi AP"),
    pulled in because the customer replied in that thread. Not a carrier invoice."""
    n = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    n = " ".join(n.split())
    return "demo" in n and ("logistics" in n or "demo logistics" in n)


def _forward_to_dispatch(zoho, msg_id, load_no, subject, from_addr, recv_date) -> bool:
    """Forward a verified carrier invoice to dispatch as a REAL threaded chain.

    Uses Zoho's reply action (the only working threading mechanism — see
    ZohoMailClient.forward_threaded) so dispatch sees the quoted original email
    below our note, with every original attachment carried over. Zoho saves the
    copy to Sent automatically.

    Gated on ZOHO_SMTP_PASSWORD being set — kept as the "this mailbox is
    configured to send" signal so forwarding only fires in the real environment,
    even though the send itself now goes over the OAuth REST API (no SMTP)."""
    if not SMTP_PASSWORD:
        logger.warning("  Load {:>5}  ⚠  forwarding not configured (ZOHO_SMTP_PASSWORD unset) — skipped", load_no)
        return False

    # Minimal note — subject + the auto-quoted original chain carry the context.
    note = "<p style='color:#888;font-size:12px;'>Sent via Demo Automated Agent</p>"
    try:
        new_id = zoho.forward_threaded(
            message_id=msg_id,
            to_address=FORWARD_TO,
            subject=f"Fwd: {subject}",
            note_html=note,
        )
        return bool(new_id)
    except Exception as exc:
        logger.error("  Threaded forward failed: {}", exc)
        return False

INVOICE_KEYWORDS = {
    "invoice", "inv", "payment", "bill", "statement", "freight",
    "load", "haul", "transport", "remittance", "receipt", "carrier",
    "trucking", "pod", "shipment", "due", "factoring", "remit",
    "factor", "funding",
}

# Subjects that mean the email has NO actual invoice — skip entirely
# Definitive non-invoices: these override even an invoice keyword in the subject.
# "Statement of Account" is a payment reminder/summary, not a single invoice —
# distinct from "Debtor Statements" (J D Factors) which carries real invoices.
_DEFINITIVE_SKIP_PHRASES = [
    "statement of account", "account statement", "aging report", "aging statement",
    "past due reminder", "overdue reminder", "payment reminder",
]

_SKIP_ONLY_PHRASES = [
    "payment inquiry", "payment status", "payment follow", "follow up on payment",
    "following up on payment", "payment update", "check on payment", "checking on payment",
    "void cheque", "void check", "bank letter", "direct deposit",
    "wire transfer details", "eft details", "banking info", "banking information",
]

# Matches pure reference subjects: "5651", "INV4162", "M4037", "HT532-A"
_SUBJECT_IS_REF = re.compile(r"^\s*[A-Z]{0,5}[-#]?\d{3,7}\s*$", re.I)

# Factoring Notice of Assignment / release letters. These are SETUP paperwork
# (a carrier assigning its receivables to a factor, or releasing that assignment),
# not an invoice. \bnoa\b won't match inside words like "noah"/"canoa".
_NOA_SUBJECT = re.compile(
    r"\b(noa|notice of assignment|letter of release|release of assignment|"
    r"assignment of (?:account|receivable)s?)\b", re.I)


def _is_noa_email(subject: str, att_names: list[str]) -> bool:
    """True if this email is a factoring Notice of Assignment / release letter —
    setup paperwork, NOT an invoice. Trying to OCR a scanned NOA is what produces
    the 'Scanned PDF could not be read' errors on the sheet.

    Yields to an explicit invoice signal so a NOA that rides along with a real
    invoice email still gets processed normally."""
    s_low = (subject or "").strip().lower()
    if "invoice" in s_low:
        return False
    if _NOA_SUBJECT.search(s_low):
        return True
    names = " ".join(att_names or []).lower()
    if _NOA_SUBJECT.search(names) and not any(
        k in names for k in ("invoice", "freight bill", "rate", "load conf")
    ):
        return True
    return False


def _is_non_invoice_email(subject: str, att_names: list[str]) -> bool:
    """Return True if this email is ONLY a payment inquiry, void cheque, or has no invoice.
    These should be skipped entirely — no processing, no forwarding."""
    s_low = subject.strip().lower()

    # Definitive non-invoices (reminders/statements) — skip even if an invoice
    # keyword is also present. Checked BEFORE the invoice-keyword rescue.
    for phrase in _DEFINITIVE_SKIP_PHRASES:
        if phrase in s_low:
            return True

    # If subject has clear invoice signals, never skip regardless of other keywords
    invoice_subject_keywords = {"invoice", "inv", "load", "freight", "pod", "bill", "statement", "factoring", "remit"}
    if any(kw in s_low for kw in invoice_subject_keywords):
        return False

    # Subject is purely a skip phrase (no invoice content mixed in)
    for phrase in _SKIP_ONLY_PHRASES:
        if phrase in s_low:
            return True

    # All attachments look like banking/admin docs, not invoices
    invoice_att_keywords = {"invoice", "inv", "load", "freight", "pod", "bill", "rate", "conf"}
    skip_att_keywords    = {"void cheque", "void check", "bank letter", "direct deposit", "void void"}
    if att_names:
        att_str = " ".join(att_names).lower()
        has_invoice_att = any(kw in att_str for kw in invoice_att_keywords)
        has_skip_att    = any(kw in att_str for kw in skip_att_keywords)
        if has_skip_att and not has_invoice_att:
            return True

    return False


def _has_invoice_subject(subject: str) -> bool:
    """Return True if this subject looks like a carrier invoice.

    Three signals — any one is enough:
    1. A keyword appears ANYWHERE in the subject (substring, not word-only),
       so "INV4162" matches because "inv" is a substring.
    2. The subject is purely a reference number like "5651" or "INV4162".
    3. Fallback — returns False, so the email is skipped.
    """
    s = subject.strip()
    s_low = s.lower()

    # Signal 1: keyword substring
    if any(kw in s_low for kw in INVOICE_KEYWORDS):
        return True

    # Signal 2: subject is just a load/invoice ref number
    if _SUBJECT_IS_REF.match(s):
        return True

    return False


# Legal suffixes and the "doing business as" / "operating as" connectors that
# split a name into a trade name and a legal entity. Stripping these lets us
# compare the meaningful tokens.
_LEGAL_SUFFIX  = re.compile(
    r"\b(ltd|inc|llc|llp|l\.?l\.?c|corp|co|limited|incorporated|company|corporation)\b", re.I)
_DBA_CONNECTOR = re.compile(
    r"\b(d\.?\s*b\.?\s*a|o\s*/?\s*b|o\s*/?\s*a|operating\s+as|trading\s+as|t\.?\s*a)\b", re.I)

# Carrier-name fuzzy threshold. Conservative: a name match alone never auto-verifies
# (amount AND currency must also align), so 88 trades a few manual reviews for never
# auto-matching two genuinely different carriers.
_NAME_FUZZ_THRESHOLD = 88


def _company_numbers(s: str) -> set[str]:
    """Corporate registration numbers (6–9 digit runs) embedded in a name, e.g.
    '1271902' in '1271902 ALBERTA INC'. A shared one is a near-certain match."""
    return set(re.findall(r"\d{6,9}", s or ""))


def _normalize_name(name: str) -> str:
    """Lowercase; drop DBA/o-b connectors, legal suffixes, and punctuation."""
    name = (name or "").lower()
    name = _DBA_CONNECTOR.sub(" ", name)
    name = _LEGAL_SUFFIX.sub(" ", name)
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    return " ".join(name.split())


def _compact(name: str) -> str:
    """Alphanumeric-only, no spaces — so 'freight line' == 'freightline'."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _names_match(a: str, b: str, threshold: int = _NAME_FUZZ_THRESHOLD) -> bool:
    """True if two carrier names refer to the same company, tolerating legal
    suffixes, DBA/operating-as wording, word order, spacing and minor spelling.

    Layered, cheapest-and-safest first:
      1. shared corporate registration number  → same entity
      2. normalized exact / containment
      3. space-insensitive exact / containment ('freight line' vs 'freightline')
      4. rapidfuzz token_set / token_sort ratio ≥ threshold (word order, typos)
    """
    if not a or not b:
        return False

    # 1. Shared corporate registration number — strongest signal
    if _company_numbers(a) & _company_numbers(b):
        return True

    na, nb = _normalize_name(a), _normalize_name(b)
    if not na or not nb:
        return False

    # 2. Normalized exact / containment
    if na == nb or na in nb or nb in na:
        return True

    # 3. Space-insensitive exact / containment (guard short strings)
    ca, cb = _compact(na), _compact(nb)
    if ca == cb:
        return True
    if len(ca) >= 5 and len(cb) >= 5 and (ca in cb or cb in ca):
        return True

    # 4. Fuzzy: word-order swaps and minor spelling differences
    try:
        from rapidfuzz import fuzz
        score = max(fuzz.token_set_ratio(na, nb), fuzz.token_sort_ratio(na, nb))
        if score >= threshold:
            return True
    except ImportError:
        pass

    return False


def _find_load_in_text(text: str, load_index: dict) -> str:
    """Scan free text for any number that matches a load number in the sheet.
    Returns the matched load number, or '' if none / ambiguous."""
    if not text:
        return ""
    nums = set(re.findall(r"\b\d{4,5}\b", text))
    matches = [n for n in nums if n in load_index]
    # Only return if exactly one sheet load number appears — avoid wrong guesses
    return matches[0] if len(matches) == 1 else ""


def parse_amount(val) -> float:
    try:
        return float(str(val).replace(",", "").replace("$", "").strip())
    except (ValueError, AttributeError):
        return 0.0


def _is_unreadable_invoice(inv_number: str, carrier_amt: float, line_haul) -> bool:
    """True when an email yielded no usable invoice AMOUNT. A real carrier invoice
    always owes a positive dollar figure, so if we couldn't read one we must not
    write (or red-flag) the load row off it. NOTE: a stray number is NOT enough —
    a Bill of Lading carries a long tracking number but $0 owed (the 5683 class,
    where the BOL number got mistaken for the invoice number); requiring an amount
    rejects those. (Was the '5555 class': bare payment-request notes / statements.)"""
    return (
        not (carrier_amt and carrier_amt > 0)
        and not (line_haul and parse_amount(line_haul) > 0)
    )


_TZ = ZoneInfo("America/Toronto")


def epoch_to_date(ts: str) -> str:
    """Email received date in Toronto local time (MM/DD/YYYY). Local, NOT UTC —
    an email received in the evening EDT is the NEXT day in UTC, which was writing
    the wrong 'IN received' date (off-by-one)."""
    try:
        return datetime.fromtimestamp(int(str(ts)[:10]), _TZ).strftime("%m/%d/%Y")
    except (ValueError, OSError):
        return ""


def _norm_invoice_date(raw) -> str:
    """Normalize a carrier invoice's own printed date to MM/DD/YYYY. The invoice
    date is stable (it's on the document) whereas the email-received date depends
    on which email happened to write the row — a re-send or the later shipper
    invoice would push 'IN received' days forward. Returns '' if unparseable."""
    s = str(raw or "").strip()
    if not s:
        return ""
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return ""


# Default carrier-extraction prompt. The live text is editable in the
# `agent_prompts` table (key "carrier_extraction"); this is the fallback used when
# the DB is unreachable or the key hasn't been seeded. The runtime fills three
# tokens: __RATE_PRESENT_LINE__, __RATE_SECTION__, __RAW_TEXT__ (see
# _build_carrier_prompt). Keep this in sync with the DB copy.
DEFAULT_CARRIER_PROMPT = """You are an expert accounts-payable analyst at Demo Logistics Inc.,
a freight BROKER. You read ONE freight document at a time and return structured JSON.
Accuracy matters far more than speed — reason carefully before you answer. When a value
is not clearly present, return null instead of guessing.
__RATE_PRESENT_LINE__

Return ONLY valid JSON — no prose, no markdown fences.

════════════════════════════════════════════════════════════════════════
STEP 1 — CLASSIFY THE DOCUMENT  (set "document_type"; this is the most important step)
════════════════════════════════════════════════════════════════════════
Decide which ONE type this document is:

A) "carrier_invoice"  — a THIRD-PARTY trucking company (the carrier), or its factoring
   company, billing Demo for hauling a load. Demo is the BILL-TO / customer here.
   This is the ONLY type we process and pay. Proceed to Step 2.

B) "broker_shipper_invoice" — Demo's OWN invoice billing one of Demo's CUSTOMERS
   (the shipper, e.g. a mill or manufacturer such as Interfor). Here Demo Logistics is
   the company ISSUING the invoice (seller / "remit to" / letterhead), and the bill-to is
   the customer. These are typically sent from dispatch@demologistics.com, open with
   "Hi AP", and follow a fixed Demo template. They are handled by a different process —
   do NOT treat them as carrier invoices.
   DECISIVE TEST: if the party being PAID (the "remit to" / issuer) is Demo Logistics,
   it is type B. A genuine carrier invoice NEVER names Demo Logistics as the carrier.

C) "not_an_invoice" — anything that is not a single carrier invoice for one load:
   account statements, remittance advice, payment / aging summaries, "freight payment
   summary", a Notice of Assignment (NOA) with no invoice attached, void cheques or
   banking-change letters, payment-status / past-due inquiries, or any document that
   lists MANY loads at once (a summary or remittance, not one load's invoice).
   ALSO type C — support documents that travel WITH an invoice but are NOT the invoice:
     • Bill of Lading (BOL) — header says "Bill of Lading", has a "Bill of Lading Number"
       and a "Carrier Name:" field. That long BOL number is NOT an invoice number, and a
       BOL "Carrier Name:" value (often a shipping term like "DELIVERY", "LTL", "FTL") is
       NOT the carrier. A BOL shows NO dollar amount owed.
     • Proof of Delivery (POD) / delivery receipt / signed receipt.
     • Packing slip, scale ticket, lumper receipt, rate/load confirmation.
   A real carrier invoice ALWAYS has: the word "Invoice", an "Invoice #"/"Invoice No",
   an "Invoice date", a "Bill To" (Demo), and a dollar Total/Amount owed. If those are
   absent, it is NOT the invoice — return type C (do not invent an invoice number from a
   BOL/tracking/reference number).

If the carrier you would otherwise extract is "Demo Logistics", the document is B.
If you are torn between A and C, choose A ONLY when there is a clear single load with an
amount owed to a NAMED third-party carrier; otherwise choose C.

FACTORING IS NOT TYPE C. A prominent "NOTICE OF ASSIGNMENT", "this invoice has been
sold / assigned", "all invoices have been assigned", or "make all payments to
<factoring company>" block does NOT demote an invoice to type C — factored invoices
are the MOST common carrier invoice we get. If the page shows a real invoice — an
Invoice #, ONE load, a Bill-To of Demo, and a single dollar amount owed — it is a
carrier_invoice (type A, is_factoring=true), no matter how large or how central the
assignment notice is, and even if it says the debt was sold. Type C's "NOA" means a
STANDALONE assignment / notice letter that carries NO invoice line and NO amount owed;
wording like "all invoices have been assigned" is boilerplate on a single invoice, NOT
a sign that the document lists many loads.

════════════════════════════════════════════════════════════════════════
STEP 2 — EXTRACT  (do this fully for type A; for B/C fill what you can and move on)
════════════════════════════════════════════════════════════════════════

OUR load number vs THEIR invoice number — never swap these:
- The LABEL next to a number is the real signal:
    OUR load number  ←  "Load #", "Load No", "PO #", "Reference", "Ref #", "Job #",
                        "BOL", "Order #", "Cust Order #", "Tender", "Voucher #"
    THEIR invoice no. ←  "Invoice #", "Inv #", "Bill #", "Statement #"
- Our load numbers are currently in the 5300–6000 range — a HINT, not a hard rule.
- A carrier's invoice number may be alphanumeric (I282466, IN-32189, M90) OR purely
  numeric (e.g. 1229125). Do NOT assume a plain number is our load number.
- NEVER place our load number in "invoice_number", and NEVER place their invoice number
  in "load_number". If you cannot confidently identify OUR load number, return null.

carrier_amount = the TOTAL the carrier is owed = line haul + accessorials, BEFORE tax:
- Accessorials ADD to the line haul: detention, layover, lumper, fuel surcharge (FSC),
  tarp, stop-off, wait time, border / customs, storage, redelivery, TONU, etc.
    Example: line haul 1400 + detention 50  →  carrier_amount 1450.
- Sales tax is NOT an accessorial and must be EXCLUDED: lines labelled HST, GST, QST,
  PST, "Tax", "5%", "13%" are tax. carrier_amount is the PRE-TAX total owed.
- If a single grand total already excludes tax, use it. List every accessorial you find
  in "extra_charges" so any discrepancy can be explained.
__RATE_SECTION__
Factoring (carriers often sell their invoice to a factoring company that collects for them):
- is_factoring = true when the document says to remit / pay a company OTHER than the
  carrier — look for "Please remit to", "Notice of Assignment", "Account assigned to",
  the word "factoring", or a remit-to address that differs from the carrier's.
- factoring_company_name = that company's name.
- factoring_company_location = its remit-to / banking address verbatim (street, suite/PO
  box, city). Capture it exactly — the SAME factoring name at a DIFFERENT address means
  DIFFERENT banking, so never normalize, shorten, or drop the address.

Currency = "CAD" or "USD": use explicit currency symbols/marks, the remit-to country, or
clear document context. If genuinely unclear, return null.

invoice_date = the date PRINTED ON the carrier's invoice (labelled "Invoice date",
"Invoice Date", "Date"). Return it as MM/DD/YYYY. This is the carrier invoice's own date,
NOT the rate-confirmation date, ship date, or due date. Null if not present.

════════════════════════════════════════════════════════════════════════
Return EXACTLY this JSON shape:
{
  "document_type": "carrier_invoice | broker_shipper_invoice | not_an_invoice",
  "is_broker_shipper_invoice": true or false,
  "load_number": "our internal load number from context labels — null if not found",
  "carrier_name": "the third-party trucking company (from the rate confirmation if present, else the invoice). If Demo Logistics is the party being paid, return Demo Logistics.",
  "invoice_number": "the PRIMARY invoice number printed at the TOP/header of the invoice (labelled Invoice #/Inv #/Bill #). If a FACTORING company issued it and the document shows BOTH a primary invoice number (the number the invoice is issued under, usually in the header) AND a separate carrier 'Client Invoice Number', use the PRIMARY/top one, NOT the client number. NEVER use the load number, order/reference number, PO number, Bill-of-Lading/PRO, or tracking number as the invoice number — if no distinct invoice number is printed, return null.",
  "invoice_date": "the invoice's printed date as MM/DD/YYYY — null if not present",
  "line_haul_amount": "base freight rate before accessorials/tax, null if not itemized",
  "carrier_amount": 0.00,
  "extra_charges": [{"label": "detention", "amount": 50.00}],
  "currency": "CAD or USD or null",
  "rate_conf_amount": "agreed amount from the rate confirmation if present, else null",
  "is_factoring": true or false,
  "factoring_company_name": null,
  "factoring_company_location": null,
  "invoice_matches_rate_conf": "true / false / null if no rate confirmation"
}

MULTIPLE INVOICES: this is uncommon, but if the document (or email) contains
SEVERAL DISTINCT carrier invoices — different load numbers / different invoice
numbers billed together (e.g. a factoring batch, or a carrier billing several
loads at once) — return a JSON ARRAY with one object of the shape above per
invoice. If there is only one invoice, return a single object (not an array).
A single invoice that merely lists several line items or accessorials is ONE
invoice, not many — only split when the load/invoice numbers genuinely differ.

Document text:
---
__RAW_TEXT__
---"""


def _build_carrier_prompt(raw_text: str, rate_text: str) -> str:
    """Fill the editable carrier-extraction template. Output is identical to the
    old hardcoded f-string; only the source of the template text changed."""
    rate_present_line = (
        "A Rate & Load Confirmation (Demo's document) is included — use it as the "
        "authoritative source for carrier name and agreed amount." if rate_text else "")
    rate_section = (
        f"\nRate & Load Confirmation (Demo's own document — authoritative):\n---\n{rate_text[:3000]}\n---\n"
        if rate_text else "")
    template = get_prompt("carrier_extraction", DEFAULT_CARRIER_PROMPT)
    return (template
            .replace("__RATE_PRESENT_LINE__", rate_present_line)
            .replace("__RATE_SECTION__", rate_section)
            .replace("__RAW_TEXT__", raw_text[:5000]))


def _ai_read(ai, prompt: str, page_imgs: list[str] | None = None) -> list[dict]:
    """One extraction call → LIST of invoice dicts ([] if nothing parseable).

    A single document returns one invoice; a document carrying several distinct
    carrier invoices (different loads) returns several — we keep them ALL.

    If page_imgs is given, sends them as image blocks (scanned PDF) before the
    prompt; otherwise sends the prompt as plain text (text PDF)."""
    if page_imgs:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}}
            for img in page_imgs
        ]
        content.append({"type": "text", "text": prompt})
    else:
        content = prompt
    try:
        resp = ai.messages.create(
            model="claude-sonnet-5", max_tokens=1024,
            # Sonnet 5 thinks by default; on long invoices it spends the whole
            # token budget thinking and emits no JSON. Extraction is a structured
            # task — disable thinking for reliable, cheap, deterministic output.
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": content}],
        )
        raw = "".join(getattr(b, "text", "") or "" for b in resp.content
                      if getattr(b, "type", "text") != "thinking").strip()
        if raw.startswith("```"):
            raw = "\n".join(l for l in raw.split("\n") if not l.startswith("```")).strip()
        data = json.loads(raw)
        return _as_invoice_list(data)
    except Exception as exc:
        logger.warning("  AI extraction failed: {}", exc)
        return []


def _as_invoice_list(data) -> list[dict]:
    """Normalise an extraction payload into a list of invoice dicts. Accepts a bare
    dict, a JSON array, or a dict wrapping the array under an 'invoices' key."""
    if isinstance(data, dict):
        if isinstance(data.get("invoices"), list):
            data = data["invoices"]
        else:
            data = [data]
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def _read_invoice_pdf(zoho, ai, msg_id, att, rate_att):
    """Download + read one invoice PDF → (invoices_list, error_reason, search_text).

    invoices_list holds EVERY invoice found in the document (usually one; a
    multi-invoice PDF yields several). Uses the extraction cache so any given PDF
    is only ever sent to the API once. On a cache hit it does NO download, NO
    render, NO API call. On failure invoices_list is [] and error_reason explains
    why. search_text is the PDF's text (for the body load-number fallback) — empty
    on cache hits / scans.
    """
    att_id = att.get("attachmentId") or att.get("id", "")

    # ── Cache hit → reuse prior reading, zero cost ────────────────────────
    cached = invoice_state.get_cached_extraction(msg_id, att_id)
    if cached is not None:
        logger.info("  cached  →  reusing prior reading, no API call ('{}')",
                    att.get("attachmentName", "")[:40])
        return _as_invoice_list(cached), None, ""  # old cache entries are single dicts

    try:
        pdf_bytes = zoho.download_attachment(msg_id, att_id)
        raw_text  = extract_text_from_pdf(pdf_bytes)
    except Exception as exc:
        return [], f"PDF download/parse failed: {exc}", ""

    # Rate confirmation (authoritative) text, if a separate rate PDF is attached
    rate_text = ""
    if rate_att:
        try:
            rate_id    = rate_att.get("attachmentId") or rate_att.get("id", "")
            rate_bytes = zoho.download_attachment(msg_id, rate_id)
            rate_text  = extract_text_from_pdf(rate_bytes)
            del rate_bytes
        except Exception:
            pass

    prompt = _build_carrier_prompt(raw_text, rate_text)
    invoices: list[dict] = []

    if raw_text.strip():
        # Text PDF — pdfplumber already read every page, so one AI call sees it all
        del pdf_bytes
        invoices = _ai_read(ai, prompt)
        if not invoices:
            return [], "AI extraction failed / returned no usable invoice", ""
        search_text = raw_text
    else:
        # Scanned PDF — render and read PAGE BY PAGE, stopping as soon as a load
        # number turns up (it's sometimes on page 2+, e.g. a factoring cover page
        # followed by the carrier's invoice). Pages accumulate so the final read
        # sees the whole document; we only render as far as we need to.
        logger.info("  Scanned PDF for '{}' — reading page by page until load # found", att.get("attachmentName", ""))

        def _read_scanned(scale: float) -> list[dict]:
            page_imgs: list[str] = []
            page_no = 0
            found: list[dict] = []
            for img in iter_pdf_pages_base64(pdf_bytes, max_pages=15, scale=scale):
                page_no += 1
                page_imgs.append(img)
                candidates = _ai_read(ai, prompt, page_imgs)
                if candidates:
                    found = candidates  # keep best-effort even if not yet complete
                    # Stop only once we have BOTH a load number AND an amount — on a
                    # multi-page scan the amount is often on a later page than the
                    # load number, and stopping at the load number alone was dropping
                    # the (money-critical) amount.
                    have_load = any(str(c.get("load_number") or "").strip() for c in candidates)
                    have_amt = any(parse_amount(c.get("carrier_amount")) > 0
                                   or parse_amount(c.get("line_haul_amount")) > 0
                                   for c in candidates)
                    if have_load and have_amt:
                        logger.info("  load # + amount found on page {} (scale {}x)", page_no, scale)
                        break
                    logger.info("  page {} read — load#={} amount={} , continuing",
                                page_no, have_load, have_amt)
            return found

        invoices = _read_scanned(1.5)
        if not invoices:
            # Low-quality/faxed scan illegible at ~108 DPI — one retry at ~216 DPI
            # before giving up. Only on total failure, so the cost/memory hit is rare.
            logger.info("  nothing read at 1.5x — retrying scan at higher resolution (3.0x)")
            invoices = _read_scanned(3.0)
        del pdf_bytes
        if not invoices:
            return [], "Scanned invoice could not be read (illegible scan) — needs manual entry", ""
        search_text = ""  # images, not text — body fallback uses subject/summary

    invoice_state.cache_extraction(msg_id, att_id, invoices)
    if len(invoices) > 1:
        logger.info("  PDF read →  {} invoices: {}", len(invoices),
                    ", ".join(str(i.get("load_number") or "?") for i in invoices))
    else:
        inv = invoices[0]
        _e = f" + {len(inv.get('extra_charges') or [])} extra charge(s)" if inv.get("extra_charges") else ""
        logger.info("  PDF read →  load={}, carrier={}, amount={}{}",
                    inv.get("load_number") or "?", (inv.get("carrier_name") or "?")[:30],
                    inv.get("carrier_amount") or "?", _e)
    return invoices, None, search_text


def run_carrier_batch(
    limit: int = 500,
    dry_run: bool = False,
    progress_callback=None,
    since_ts: int = 0,
    use_watermark: bool = False,
    window_days: int = 5,
) -> dict:
    """Scan inbox for carrier invoices and update the Accounts sheet.

    Scan window (same semantics as the shipper lane):
      • use_watermark=True → scan since this lane's saved watermark (with a
        `window_days` floor) and ADVANCE it on a clean, non-dry run.
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

    run_start_ms = int(time.time() * 1000)
    if use_watermark:
        floor = run_start_ms - window_days * 86_400_000
        wm = get_watermark(LANE)
        since_ts = max(wm, floor) if wm else floor

    dry = " [DRY-RUN]" if dry_run else ""
    logger.info("── CARRIER INVOICES{} ──────────────────────────────", dry)
    report = RunReport("carrier")

    records = sheets.get_all_records(config.accounts_sheet_name)
    for i, r in enumerate(records):
        r["__row_number__"] = i + 2

    load_index: dict[str, dict] = {}
    for r in records:
        ln = str(r.get("Load no", "")).strip()
        if ln:
            load_index[ln] = r

    # ── Retry any forwards that failed on a previous run (decoupled) ──────────
    if not dry_run:
        for pending in invoice_state.list_forward_pending():
            if is_blocked_sender(pending.get("from_addr", "")):
                invoice_state.remove_forward_pending(pending["message_id"])
                logger.info("  Dropping queued forward from blocked sender: {}", pending.get("from_addr", ""))
                continue
            if invoice_state.is_load_forwarded(pending["load_no"]):
                invoice_state.remove_forward_pending(pending["message_id"])
                continue
            ok = _forward_to_dispatch(
                zoho, pending["message_id"], pending["load_no"],
                pending["subject"], pending["from_addr"], pending["recv_date"],
            )
            if ok:
                invoice_state.mark_load_forwarded(pending["load_no"], pending["message_id"])
                invoice_state.remove_forward_pending(pending["message_id"])
                logger.info("  Load {:>5}  ✓  forward retry succeeded", pending["load_no"])

    emails = zoho.list_inbox_since(since_ts) if since_ts else zoho.list_inbox(limit=limit)
    if since_ts:
        logger.info("Scanning everything since {}", datetime.fromtimestamp(since_ts / 1000).strftime("%Y-%m-%d %H:%M"))

    candidates = []
    for e in emails:
        sender  = str(e.get("fromAddress", "")).lower()
        subject = str(e.get("subject", ""))
        if sender == DISPATCH_SENDER:
            continue
        # Sensitive senders (e.g. Example Vendor A, Example Vendor B) — never process or forward.
        if is_blocked_sender(f"{e.get('fromAddress','')} {e.get('sender','')}"):
            logger.info("  Skipping sensitive/blocked sender: {}", e.get("fromAddress", ""))
            continue
        has_att = str(e.get("hasAttachment", "0")) in ("1", "true", "True")
        if not has_att:
            continue
        if not _has_invoice_subject(subject):
            continue
        candidates.append(e)

    logger.info("Scanning {} candidate emails of {} fetched…", len(candidates), len(emails))

    stats = {
        "verified": 0,
        "discrepancy": 0,
        "skipped_no_load": 0,
        "skipped_no_match": 0,
        "skipped_already_done": 0,
        "skipped_no_invoice": 0,
        "skipped_shipper_invoice": 0,
        "errors": 0,
    }

    # ── Process each candidate ────────────────────────────────────────────────
    for email in candidates:
        msg_id    = email["messageId"]
        subject   = email.get("subject", "")
        from_addr = email.get("fromAddress", "")
        recv_date = epoch_to_date(email.get("receivedTime", ""))

        # ── Get PDFs ──────────────────────────────────────────────────────
        try:
            atts = zoho.list_attachments(msg_id)
        except Exception as exc:
            logger.info("  ?      ✗  attachment error ({}): {}", subject[:30], exc)
            report.add("?", "error", f"Attachment list failed: {exc}",
                       email_from=from_addr, email_subject=subject, message_id=msg_id)
            stats["errors"] += 1
            continue

        if not atts:
            continue  # no PDF — not an invoice, skip silently (legitimate)

        att_names = [a.get("attachmentName", "") for a in atts]

        # ── Notice of Assignment / factoring release letter ───────────────────
        # A NOA is factoring setup paperwork (a carrier assigning its receivables
        # to a factor), NOT an invoice. OCRing it just produces "Scanned PDF could
        # not be read" errors on the sheet. Skip processing — but leave it UNREAD:
        # it carries remit-to/banking changes the accounts team must action.
        if _is_noa_email(subject, att_names):
            logger.info("  skipped  →  Notice of Assignment (factoring setup, not an invoice) — left UNREAD: '{}'", subject[:50])
            stats["skipped_no_invoice"] += 1
            continue

        # ── Skip if this is ONLY a payment inquiry / void cheque / no invoice ─
        if _is_non_invoice_email(subject, att_names):
            logger.info("  skipped  →  non-invoice email (payment inquiry/void cheque/no invoice): '{}'", subject[:50])
            if not dry_run:
                zoho.mark_as_read(msg_id)
            continue

        # Prefer the invoice PDF(s); also grab any rate confirmation.
        RATE_CONF_KEYWORDS = {"rate", "load confirmation", "order confirmation", "confirmation"}
        # Documents that ride ALONG with an invoice but are NOT the invoice. Reading
        # these as invoices is how a Bill of Lading number / a BOL "Carrier Name:
        # DELIVERY" field ends up on the sheet (the 5683 class). Never treat them as
        # the invoice — but keep them out only if a real invoice PDF remains.
        NON_INVOICE_ATT_KEYWORDS = {
            "bol", "bill of lading", "pod", "proof of delivery", "void", "cheque",
            "check", "banking", "deposit", "notice of assignment", "packing slip",
            "scale ticket", "lumper receipt",
        }

        def _is_support_doc(a):
            n = a.get("attachmentName", "").lower()
            return (any(k in n for k in RATE_CONF_KEYWORDS)
                    or any(k in n for k in NON_INVOICE_ATT_KEYWORDS))

        inv_atts = [a for a in atts if not _is_support_doc(a)] or [atts[0]]
        rate_att = next(
            (a for a in atts if any(k in a.get("attachmentName", "").lower() for k in RATE_CONF_KEYWORDS)),
            None,
        )

        # Read every invoice attachment; a document may carry several invoices.
        # Multi-invoice emails are uncommon but real — each invoice is processed.
        for att in inv_atts:
            att_id = att.get("attachmentId") or att.get("id", "")
            if invoice_state.is_processed(msg_id, att_id):
                stats["skipped_already_done"] += 1
                continue

            invoices, err, pdf_search_text = _read_invoice_pdf(zoho, ai, msg_id, att, rate_att)
            if not invoices:
                logger.info("  ?      ✗  {}", err)
                report.add("?", "error", err,
                           email_from=from_addr, email_subject=subject, message_id=msg_id)
                stats["errors"] += 1
                continue
            if len(invoices) > 1:
                logger.info("  Multi-invoice document — processing {} invoices", len(invoices))

            for inv in invoices:
                load_no      = str(inv.get("load_number") or "").strip()
                carrier_name = str(inv.get("carrier_name") or "").strip()
                inv_number   = str(inv.get("invoice_number") or "").strip()
                carrier_amt  = float(inv.get("carrier_amount") or 0)
                line_haul    = inv.get("line_haul_amount")
                extra_charges = inv.get("extra_charges") or []
                currency     = str(inv.get("currency") or "").upper()
                doc_type     = str(inv.get("document_type") or "").strip().lower()

                # ── Shipper-invoice guard (wrong lane) ────────────────────────────
                # SHIPPER invoices are ours: dispatch@demologistics.com → the customer,
                # "Hi AP", fixed format. They get handled by the shipper batch. When a
                # customer REPLIES in that thread (e.g. Interfor.AP@interfor.com on
                # "Invoice_5714 Load_5714"), the reply isn't from dispatch so it reaches
                # here — and the model reads OUR invoice, returning "Demo Logistics" as
                # the carrier. A real carrier invoice never names Demo as the carrier.
                # Trust the model's classification first, then the carrier-name heuristic.
                # Skip it: don't verify it, don't flag the real carrier row red.
                if (doc_type == "broker_shipper_invoice"
                        or inv.get("is_broker_shipper_invoice") is True
                        or _is_own_company(carrier_name)):
                    logger.info(
                        "  Load {:>5}  →  shipper invoice in carrier lane (carrier='{}') — skipped",
                        load_no or "?", carrier_name)
                    report.add(load_no or "?", "skipped_shipper_invoice",
                               f"Shipper invoice (Demo is the biller / carrier='{carrier_name}') "
                               "reached the carrier lane — already handled by the shipper batch; skipped",
                               email_from=from_addr, email_subject=subject, message_id=msg_id)
                    stats["skipped_shipper_invoice"] += 1
                    continue

                # ── Non-invoice guard (model-classified) ──────────────────────────
                # Statements, remittance/payment summaries, NOA-only, void cheques, or a
                # document listing many loads at once. Don't write anything off these;
                # leave UNREAD so a human looks (surfaces in the daily summary).
                if doc_type == "not_an_invoice":
                    logger.info("  Load {:>5}  →  not an invoice (statement/summary/NOA) — left unread",
                                load_no or "?")
                    report.add(load_no or "?", "no_invoice",
                               "Document classified as not a single carrier invoice "
                               "(statement / remittance / summary / NOA) — left UNREAD for manual check",
                               email_from=from_addr, email_subject=subject, message_id=msg_id)
                    stats["skipped_no_invoice"] += 1
                    continue

                is_factoring = bool(inv.get("is_factoring", False))
                fact_name    = str(inv.get("factoring_company_name") or "").strip()
                fact_loc     = str(inv.get("factoring_company_location") or "").strip()

                # ── No load number from PDF → search subject + email body ─────────
                if not load_no or load_no not in load_index:
                    # Try the PDF text, the subject, and the email body/summary
                    body_text = " ".join([
                        subject,
                        email.get("summary", ""),
                        pdf_search_text,
                    ])
                    found = _find_load_in_text(body_text, load_index)
                    if found:
                        logger.info("  Load {:>5}     recovered load # from email text", found)
                        load_no = found

                if not load_no:
                    logger.info("  ?      →  no load # found anywhere, left unread")
                    report.add("?", "no_load", "No load number found in PDF, subject, or body",
                               email_from=from_addr, email_subject=subject, message_id=msg_id)
                    stats["skipped_no_load"] += 1
                    continue

                # ── Find sheet row ────────────────────────────────────────────────
                row_data = load_index.get(load_no)
                if not row_data:
                    logger.info("  Load {:>5}  →  no sheet row, left unread", load_no)
                    report.add(load_no, "no_row", "Load not yet in Accounts sheet",
                               email_from=from_addr, email_subject=subject, message_id=msg_id)
                    stats["skipped_no_match"] += 1
                    continue

                row_num = row_data["__row_number__"]

                # ── No-readable-invoice guard ─────────────────────────────────────
                # Some emails carry no actual invoice we can read: a bare "payment
                # request" note, a statement, a forwarded body with no usable PDF.
                # Those come back with NO invoice number, $0, and no line-haul — the
                # model then guesses a currency, and we'd paint the real load row red
                # off a document we never actually read (this is the 5555 class).
                # Don't touch the load row; leave the email UNREAD so a human looks.
                if _is_unreadable_invoice(inv_number, carrier_amt, line_haul):
                    logger.info(
                        "  Load {:>5}  →  no readable invoice (no #, $0, no line-haul) — left unread",
                        load_no)
                    report.add(load_no, "no_invoice",
                               "No readable invoice in this email (no number, $0, no line-haul) "
                               "— left UNREAD for manual check",
                               sheet_row=row_num, email_from=from_addr,
                               email_subject=subject, message_id=msg_id)
                    stats["skipped_no_invoice"] += 1
                    continue

                # ── Skip if Carrier In# already filled ───────────────────────────
                if str(row_data.get("Carrier In#", "")).strip():
                    stats["skipped_already_done"] += 1
                    report.add(load_no, "skipped_done", "Carrier In# already filled",
                               sheet_row=row_num, email_from=from_addr,
                               email_subject=subject, message_id=msg_id)
                    if not dry_run:
                        zoho.mark_as_read(msg_id)
                    continue

                # ── Compare data ──────────────────────────────────────────────────
                sheet_carrier = str(row_data.get("Carrier name", "")).strip()
                sheet_amt     = parse_amount(row_data.get("Carrier Amount", ""))
                sheet_curr    = str(row_data.get("Currency", "")).upper().strip()

                inv_matches_rc = inv.get("invoice_matches_rate_conf")

                if inv_matches_rc is not None:
                    # Rate confirmation present — compare invoice vs rate conf (authoritative)
                    # Sheet discrepancy is noted separately but doesn't fail the verification
                    all_ok    = (inv_matches_rc is True or inv_matches_rc == "true")
                    name_ok   = all_ok
                    amt_ok    = all_ok
                    curr_ok   = all_ok
                    sheet_differs = (
                        not _names_match(carrier_name, sheet_carrier) or
                        (sheet_amt > 0 and carrier_amt > 0 and abs(sheet_amt - carrier_amt) > 1.0) or
                        (sheet_curr and currency and sheet_curr != currency)
                    )
                else:
                    # No rate confirmation — fall back to comparing invoice vs sheet
                    name_ok = _names_match(carrier_name, sheet_carrier) if carrier_name else True
                    amt_ok  = sheet_amt == 0 or carrier_amt == 0 or abs(sheet_amt - carrier_amt) <= 1.0
                    curr_ok = not sheet_curr or not currency or sheet_curr == currency
                    all_ok  = name_ok and amt_ok and curr_ok
                    sheet_differs = False
                    inv_matches_rc = None

                # ── Build updates ─────────────────────────────────────────────────
                updates: dict = {}

                # Carrier invoice number. NEVER fall back to the load number — that masks a
                # failed read and corrupts the column with our own load #. Blank if unread;
                # the missing-number case is flagged for review below.
                updates["Carrier In#"] = inv_number

                # Factoring yes/no
                updates["Factoring"] = "yes" if is_factoring else "no"

                # Date received — prefer the carrier invoice's OWN printed date
                # (stable, on the document) over the email-received date, which is
                # whichever email happened to write the row (a re-send or the later
                # shipper invoice was pushing this days forward). Fall back to the
                # (now Toronto-local) received date when the invoice has no date.
                date_val = _norm_invoice_date(inv.get("invoice_date")) or recv_date
                if date_val and not str(row_data.get("IN received", "")).strip():
                    updates["IN received"] = date_val

                # Factoring details (col 24)
                if is_factoring and fact_name:
                    fact_detail = fact_name
                    if fact_loc:
                        fact_detail += f", {fact_loc}"
                    updates["Factoring_2"] = fact_detail  # deduplicated header name

                fact_tag = f" (factoring: {fact_name})" if is_factoring and fact_name else ""
                compare_details = {
                    "carrier_invoice": carrier_name, "carrier_sheet": sheet_carrier,
                    "amount_invoice": carrier_amt, "amount_sheet": sheet_amt,
                    "currency_invoice": currency, "currency_sheet": sheet_curr,
                    "invoice_number": inv_number, "is_factoring": is_factoring,
                    "line_haul": line_haul, "extra_charges": extra_charges,
                }

                # Carrier invoice number missing → never silently verify with a blank number
                # (and never the load #). Flag so a human captures the real invoice number.
                if not inv_number:
                    remark = "Carrier invoice number not captured — verify & fill manually"
                    updates["Remarks"] = remark
                    logger.info("  Load {:>5}  ⚠  carrier invoice# not read — flagging for review", load_no)
                    stats["discrepancy"] += 1
                    final_status = "discrepancy"
                    final_reason = remark
                # Accessorial fees present → always flag for manual check, do not auto-verify
                elif extra_charges:
                    charge_str = ", ".join(
                        f"{str(c.get('label','fee')).strip()} ${float(c.get('amount') or 0):g}"
                        for c in extra_charges
                        if c and float(c.get("amount") or 0) > 0
                    )
                    remark = f"Has extra charges ({charge_str}) — verify total manually"
                    updates["Remarks"] = remark
                    logger.info("  Load {:>5}  ⚠  Accessorial fees detected — flagging for review: {}", load_no, charge_str)
                    stats["discrepancy"] += 1
                    final_status = "discrepancy"
                    final_reason = remark
                elif all_ok:
                    remark = "Carrier invoice verified ✓"
                    if sheet_differs:
                        remark += (
                            f" (rate conf matches invoice, but sheet entry differs — "
                            f"sheet shows '{sheet_carrier}' ${sheet_amt} {sheet_curr}, "
                            f"correct is '{carrier_name}' ${carrier_amt} {currency} — fix cols K/P/Q)"
                        )
                    updates["Remarks"] = remark
                    icon = "✓" if not sheet_differs else "✓⚠"
                    logger.info("  Load {:>5}  {}  Carrier verified — {}{}", load_no, icon, carrier_name, fact_tag)
                    stats["verified"] += 1
                    final_status = "discrepancy" if sheet_differs else "verified"
                    final_reason = remark if sheet_differs else ""
                else:
                    notes = []
                    if not name_ok:
                        notes.append(f"Carrier: invoice='{carrier_name}' vs sheet='{sheet_carrier}'")
                    if not amt_ok and carrier_amt > 0:
                        notes.append(f"Amount: invoice=${carrier_amt} vs sheet=${sheet_amt}")
                    if not curr_ok:
                        notes.append(f"Currency: invoice={currency} vs sheet={sheet_curr}")
                    # Prefix so a human instantly knows WHICH invoice is in question —
                    # the shipper batch writes to the same Remarks column.
                    updates["Remarks"] = "CARRIER — " + " | ".join(notes)
                    logger.info("  Load {:>5}  ⚠  Carrier DISCREPANCY — {}", load_no, updates["Remarks"])
                    stats["discrepancy"] += 1
                    final_status = "discrepancy"
                    final_reason = updates["Remarks"]

                if dry_run:
                    logger.info("  Load {:>5}  [DRY-RUN] would write {}", load_no, list(updates.keys()))
                    report.add(load_no, final_status, final_reason, sheet_row=row_num,
                               email_from=from_addr, email_subject=subject,
                               message_id=msg_id, details=compare_details)
                    continue

                # ── Stage 1: write to sheet ───────────────────────────────────────
                try:
                    sheets.update_cells_by_header(config.accounts_sheet_name, row_num, updates)
                    sheets.bold_cell_by_header(config.accounts_sheet_name, row_num, "Carrier In#")
                    if not all_ok or extra_charges:
                        sheets.highlight_row_magenta(config.accounts_sheet_name, row_num)
                    elif sheet_differs:
                        sheets.highlight_row_orange(config.accounts_sheet_name, row_num)
                except Exception as exc:
                    # Sheet write failed — do NOT mark read/processed, retry next run
                    logger.info("  Load {:>5}  ✗  sheet write failed (will retry): {}", load_no, exc)
                    report.add(load_no, "error", f"Sheet write failed: {exc}", sheet_row=row_num,
                               email_from=from_addr, email_subject=subject, message_id=msg_id)
                    stats["errors"] += 1
                    continue

                # ── Stage 2: sheet succeeded → mark read + record state ───────────
                zoho.mark_as_read(msg_id)
                invoice_state.mark_processed(
                    msg_id, att_id,
                    invoice_number=inv_number, carrier_name=carrier_name, sheet_row=row_num,
                )

                # ── Stage 3: forward to dispatch ONLY when verified ─────────────────
                # Discrepancies and accessorial flags stay internal (red on sheet + dashboard).
                # Accounts team handles those — dispatch only needs clean, verified invoices.
                forwarded = None
                if final_status == "verified":
                    if invoice_state.is_load_forwarded(load_no):
                        logger.info("  Load {:>5}     already forwarded to dispatch — skipping duplicate", load_no)
                    elif zoho.dispatch_notified(email):
                        logger.info("  Load {:>5}     dispatch already on thread", load_no)
                        invoice_state.mark_load_forwarded(load_no, msg_id)
                    else:
                        forwarded = _forward_to_dispatch(zoho, msg_id, load_no, subject, from_addr, recv_date)
                        if forwarded:
                            invoice_state.mark_load_forwarded(load_no, msg_id)
                            logger.info("  Load {:>5}  ✓  forwarded to dispatch", load_no)
                        else:
                            invoice_state.add_forward_pending(msg_id, load_no, subject, from_addr, recv_date)
                            logger.info("  Load {:>5}  ⚠  forward failed — queued for retry", load_no)
                else:
                    logger.info("  Load {:>5}     not forwarding — {} (accounts team to review)", load_no, final_status)

                report.add(load_no, final_status, final_reason, sheet_row=row_num,
                           email_from=from_addr, email_subject=subject, message_id=msg_id,
                           forwarded=forwarded, details=compare_details)

    # ── Summary ───────────────────────────────────────────────────────────────
    logger.info("─" * 52)
    logger.info(
        "  ✓ {}  ⚠ {}  → {} no load#  → {} no row  → {} no invoice  → {} shipper-inv  → {} already done  ✗ {} errors",
        stats["verified"], stats["discrepancy"], stats["skipped_no_load"],
        stats["skipped_no_match"], stats["skipped_no_invoice"],
        stats["skipped_shipper_invoice"], stats["skipped_already_done"], stats["errors"],
    )

    if not dry_run:
        report.finish()

    # Advance this lane's watermark so the next run starts where this one finished.
    if use_watermark and not dry_run:
        set_watermark(run_start_ms, LANE)
        logger.info("Watermark ({}) advanced — next run starts here.", LANE)

    if sink_id is not None:
        logger.remove(sink_id)

    stats["_report_items"] = report.items  # for the audit harness (per-load detail)
    return stats


if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()

    _log = logger
    _log.remove()
    _log.add(sys.stderr, level="INFO",
             format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")

    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    run_carrier_batch(limit=args.limit, dry_run=args.dry_run)
