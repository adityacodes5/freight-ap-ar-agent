"""
Demo Logistics – Phase 1 AI Agent

Reads shipper invoices from accounts@demologistics.com (Zoho Mail) and
updates the Google Sheets "Accounts" tab.

Workflow per email / invoice:
  1. Find the matching row in the sheet by Load no or Customer Name.
  2. Copy Load no → Invoice no (col 2) if not already filled.
  3. Confirm Customer Name on invoice matches the sheet (col 4).
  4. Fill IN received (col 14) with the email received date.
  5. Fill HST (col 7) with any tax shown on the invoice.
  6. Confirm Shipper Amt (col 6) matches the rate on the invoice.
  7. If everything matches → STATUS: verified.
     If any discrepancy → write note in Remarks (col 25), highlight row RED.
"""

import json
from datetime import datetime, timedelta

import anthropic
from loguru import logger

from config import Config
from tools.zoho_mail import ZohoMailClient
from tools.sheets import GoogleSheetsClient
from tools.parser import extract_text_from_pdf
import state as invoice_state

# ── Exact column headers from the Accounts sheet ─────────────────────────────
#   Col 1  Load no
#   Col 2  Invoice no
#   Col 3  ship. P.O. Number
#   Col 4  Customer Name
#   Col 5  invoice date
#   Col 6  Shipper Amt
#   Col 7  HST
#   Col 8  Due date
#   Col 9   client Cheque no
#   Col 10 PMT Revd
#   Col 11 Carrier name
#   Col 12 Carrier In#
#   Col 13 Factoring
#   Col 14 IN received
#   Col 15 Payment due date
#   Col 16 Carrier Amount
#   Col 17 Currency
#   Col 20 STATUS
#   Col 25 Remarks

# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "list_inbox_emails",
        "description": (
            "List the most recent emails from the accounts@demologistics.com inbox. "
            "Returns emails with messageId, subject, fromAddress, received_date, hasAttachment. "
            "Use this to find incoming shipper invoices."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "How many recent emails to fetch (default 50)",
                    "default": 50,
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_email_attachments",
        "description": (
            "Get the PDF attachments for a specific email. "
            "Returns [{attachmentId, attachmentName, size}]. "
            "Only PDFs are returned."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"}
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "extract_invoice_data",
        "description": (
            "Download a PDF attachment and extract structured invoice data using AI. "
            "Returns: invoice_number, invoice_date, customer_name, load_number, "
            "shipper_amount, hst_amount, currency, total_amount, po_number. "
            "Also returns __message_id__ and __attachment_id__ for later use."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "attachment_id": {"type": "string"},
                "attachment_name": {"type": "string"},
                "email_received_date": {
                    "type": "string",
                    "description": "YYYY-MM-DD date of the email",
                },
            },
            "required": ["message_id", "attachment_id"],
        },
    },
    {
        "name": "read_accounts_sheet",
        "description": (
            "Read all rows from the Accounts sheet. "
            "Returns {headers, records} where each record has __row_number__ (1-based). "
            "Key columns: 'Load no', 'Invoice no', 'Customer Name', 'Shipper Amt', "
            "'HST', 'IN received', 'Payment due date ', 'Carrier In#', 'Remarks', 'STATUS'."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "update_sheet_row",
        "description": (
            "Update specific columns in a row of the Accounts sheet. "
            "Pass updates as {column_header: value}. "
            "Only update columns that are currently EMPTY — never overwrite existing data. "
            "Standard fields to always include when writing a verified invoice: "
            "\"Invoice no\", \"invoice date\" (MM/DD/YYYY from PDF), "
            "\"IN received\" (MM/DD/YYYY email date), "
            "\"Payment due date \" (30 days after IN received), "
            "\"HST\", \"STATUS\", \"Remarks\""
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "row_number": {
                    "type": "integer",
                    "description": "1-based row number (__row_number__ from read_accounts_sheet)",
                },
                "updates": {
                    "type": "object",
                    "description": "Dict of {column_header: value} to write",
                },
            },
            "required": ["row_number", "updates"],
        },
    },
    {
        "name": "highlight_row_red",
        "description": (
            "Highlight an entire row RED in the Accounts sheet. "
            "Use this when invoice data does not match the pre-entered sheet data "
            "(customer name mismatch, rate discrepancy, etc.)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "row_number": {
                    "type": "integer",
                    "description": "1-based row number to highlight",
                }
            },
            "required": ["row_number"],
        },
    },
    {
        "name": "mark_invoice_processed",
        "description": (
            "Call this after successfully processing an invoice (whether matched or flagged). "
            "Prevents reprocessing the same email/PDF on the next run. "
            "Use __message_id__ and __attachment_id__ from the extract_invoice_data result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "attachment_id": {"type": "string"},
                "invoice_number": {"type": "string"},
                "customer_name": {"type": "string"},
                "sheet_row": {"type": "integer"},
                "status": {
                    "type": "string",
                    "description": "verified, discrepancy, or no_match",
                },
            },
            "required": ["message_id", "attachment_id"],
        },
    },
]

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an accounts assistant for Demo Logistics, a Canadian freight brokerage.

You read shipper invoices from the accounts@demologistics.com inbox and update the Google Sheets "Accounts" tab.

## The Accounts sheet columns (exact names)
- Col 1  "Load no"          — pre-filled internal order number (e.g. 4779)
- Col 2  "Invoice no"       — invoice number to fill in (usually same as Load no)
- Col 4  "Customer Name"    — shipper/customer name to VERIFY against invoice
- Col 6  "Shipper Amt"      — rate to VERIFY against invoice
- Col 7  "HST"              — HST/tax amount to fill in from invoice
- Col 5  "invoice date"     — invoice date from the PDF (fill in, MM/DD/YYYY)
- Col 14 "IN received"      — date we received this invoice / email (fill with email date, MM/DD/YYYY)
- Col 15 "Payment due date "— 30 days after IN received (fill in, MM/DD/YYYY)
- Col 20 "STATUS"           — write "Verified" when confirmed OK
- Col 25 "Remarks"          — write discrepancy notes here

## Workflow for each invoice email

1. **list_inbox_emails** — Get recent inbox emails.

2. **get_email_attachments** — For each email that looks like it contains a shipper invoice PDF, get attachments.

3. **extract_invoice_data** — Extract: invoice_number, customer_name, load_number, shipper_amount, hst_amount, invoice_date from the PDF.
   - If extract returns {"skipped": true}, skip it (already processed).

4. **read_accounts_sheet** — Load the full sheet (do this ONCE and reuse for multiple invoices).

5. **Match the row** — Find the row where:
   - "Load no" matches the load_number on the invoice, OR
   - "Customer Name" closely matches customer_name on the invoice.

6. **Compare data**:
   - Does Customer Name on invoice match "Customer Name" in sheet? (case-insensitive, allow minor differences)
   - Does Shipper Amt on invoice match "Shipper Amt" in sheet? (allow ±$1 rounding)

7. **If everything matches**:
   - update_sheet_row with:
     - "Invoice no" = invoice_number (if col 2 is empty, else skip)
     - "invoice date" = invoice_date from PDF (MM/DD/YYYY, if not already filled)
     - "IN received" = email received date (MM/DD/YYYY)
     - "Payment due date " = received date + 30 days (MM/DD/YYYY)
     - "HST" = hst_amount from invoice (if not already filled)
     - "STATUS" = "Verified"
     - "Remarks" = "Invoice verified ✓"

8. **If there is a discrepancy** (name mismatch OR rate mismatch):
   - update_sheet_row with:
     - "invoice date" = invoice_date from PDF (MM/DD/YYYY, if not already filled)
     - "IN received" = email received date
     - "Remarks" = specific note e.g. "DISCREPANCY: Invoice shows $3500, sheet has $3200"
   - highlight_row_red with that row number

9. **mark_invoice_processed** after each invoice, whether matched or flagged.

## Rules
- NEVER overwrite a cell that already has data.
- Read_accounts_sheet once, reuse for all invoices in the same run.
- Amounts: strip $ signs and commas before comparing numbers.
- Dates: store as MM/DD/YYYY to match existing sheet format.
- At the end, give a clear summary: how many invoices processed, verified, flagged, skipped.
"""

# ── Agent class ───────────────────────────────────────────────────────────────


class DemoAgent:
    def __init__(self, config: Config, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        self.zoho = ZohoMailClient(config)
        self.sheets = GoogleSheetsClient(config)
        self._sheet_cache: dict | None = None
        if dry_run:
            logger.info("DRY-RUN mode — no writes will be made")

    # ── Tool dispatcher ───────────────────────────────────────────────────────

    def _execute_tool(self, name: str, inp: dict) -> str:
        try:
            match name:
                case "list_inbox_emails":
                    return self._list_inbox(**inp)
                case "get_email_attachments":
                    return self._get_attachments(**inp)
                case "extract_invoice_data":
                    return self._extract(**inp)
                case "read_accounts_sheet":
                    return self._read_sheet()
                case "update_sheet_row":
                    return self._update_row(**inp)
                case "highlight_row_red":
                    return self._highlight_red(**inp)
                case "mark_invoice_processed":
                    return self._mark_done(**inp)
                case _:
                    return json.dumps({"error": f"Unknown tool: {name}"})
        except Exception as exc:
            logger.exception("Tool '{}' failed", name)
            return json.dumps({"error": str(exc)})

    # ── Tool implementations ──────────────────────────────────────────────────

    def _list_inbox(self, limit: int = 50) -> str:
        emails = self.zoho.list_inbox(limit=limit)
        out = []
        for e in emails:
            # Convert epoch ms → YYYY-MM-DD
            received_date = ""
            raw_ts = e.get("receivedTime") or e.get("sentDateInGMT", "")
            if raw_ts:
                try:
                    ts = int(str(raw_ts)[:10])
                    received_date = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                except (ValueError, OSError):
                    received_date = str(raw_ts)[:10]
            out.append({
                "messageId": e.get("messageId", ""),
                "subject": e.get("subject", ""),
                "fromAddress": e.get("fromAddress", ""),
                "received_date": received_date,
                "hasAttachment": e.get("hasAttachment", False),
                "summary": (e.get("summary") or "")[:150],
            })
        return json.dumps({"count": len(out), "emails": out})

    def _get_attachments(self, message_id: str) -> str:
        attachments = self.zoho.list_attachments(message_id)
        return json.dumps({"count": len(attachments), "attachments": attachments})

    def _extract(
        self,
        message_id: str,
        attachment_id: str,
        attachment_name: str = "",
        email_received_date: str = "",
    ) -> str:
        if invoice_state.is_processed(message_id, attachment_id):
            logger.info("Skipping already-processed: {}", attachment_name)
            return json.dumps({"skipped": True, "reason": "already processed"})

        pdf_bytes = self.zoho.download_attachment(message_id, attachment_id)
        if not pdf_bytes:
            return json.dumps({"error": "Empty attachment"})

        raw_text = extract_text_from_pdf(pdf_bytes)
        if not raw_text.strip():
            return json.dumps({"error": "No text extracted from PDF"})

        prompt = f"""Extract invoice data from this freight/shipper invoice.
Return ONLY a JSON object — no other text:
{{
  "invoice_number": "...",
  "invoice_date": "YYYY-MM-DD or null",
  "customer_name": "...",
  "load_number": "...",
  "po_number": "...",
  "shipper_amount": 0.00,
  "hst_amount": 0.00,
  "total_amount": 0.00,
  "currency": "CAD or USD"
}}

Invoice text:
---
{raw_text[:8000]}
---"""

        resp = self.client.messages.create(
            model="claude-sonnet-5",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(getattr(b, "text", "") or "" for b in resp.content
                      if getattr(b, "type", "text") != "thinking").strip()
        if raw.startswith("```"):
            raw = "\n".join(l for l in raw.split("\n") if not l.startswith("```")).strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return json.dumps({"error": "Parse failed", "raw": raw[:300]})

        data["email_received_date"] = email_received_date
        data["__message_id__"] = message_id
        data["__attachment_id__"] = attachment_id

        # Format invoice date as MM/DD/YYYY
        raw_inv_date = data.get("invoice_date") or ""
        if raw_inv_date:
            try:
                data["invoice_date_formatted"] = datetime.strptime(
                    raw_inv_date[:10], "%Y-%m-%d"
                ).strftime("%m/%d/%Y")
            except ValueError:
                data["invoice_date_formatted"] = raw_inv_date

        # Pre-calculate 30-day deadline from email received date
        if email_received_date:
            try:
                dt = datetime.strptime(email_received_date[:10], "%Y-%m-%d")
                data["payment_deadline"] = (dt + timedelta(days=30)).strftime("%m/%d/%Y")
                data["received_formatted"] = dt.strftime("%m/%d/%Y")
            except ValueError:
                pass

        logger.info(
            "Extracted: {} | {} | ${} {}",
            data.get("invoice_number"), data.get("customer_name"),
            data.get("shipper_amount"), data.get("currency"),
        )
        return json.dumps(data)

    def _read_sheet(self) -> str:
        if self._sheet_cache is None:
            headers = self.sheets.get_headers(self.config.accounts_sheet_name)
            records = self.sheets.get_all_records(self.config.accounts_sheet_name)
            for i, rec in enumerate(records):
                rec["__row_number__"] = i + 2  # row 1 = headers
            self._sheet_cache = {
                "headers": headers,
                "records": records,
                "row_count": len(records),
            }
        return json.dumps(self._sheet_cache)

    def _update_row(self, row_number: int, updates: dict) -> str:
        if self.dry_run:
            logger.info("[DRY-RUN] Row {} would get: {}", row_number, updates)
            return json.dumps({"status": "dry-run", "row": row_number, "updates": updates})
        self.sheets.update_cells_by_header(
            self.config.accounts_sheet_name, row_number, updates
        )
        self._sheet_cache = None  # invalidate cache
        return json.dumps({"status": "ok", "row": row_number, "updated": list(updates.keys())})

    def _highlight_red(self, row_number: int) -> str:
        if self.dry_run:
            logger.info("[DRY-RUN] Row {} would be highlighted red", row_number)
            return json.dumps({"status": "dry-run", "row": row_number})
        self.sheets.highlight_row_red(self.config.accounts_sheet_name, row_number)
        return json.dumps({"status": "ok", "row": row_number, "highlighted": "red"})

    def _mark_done(
        self,
        message_id: str,
        attachment_id: str,
        invoice_number: str = "",
        customer_name: str = "",
        sheet_row: int | None = None,
        status: str = "",
    ) -> str:
        if not self.dry_run:
            invoice_state.mark_processed(
                message_id, attachment_id, invoice_number, customer_name, sheet_row
            )
        return json.dumps({"status": "ok", "recorded": status})

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run_phase1(self, dry_run: bool | None = None) -> str:
        if dry_run is not None:
            self.dry_run = dry_run

        dry_note = " (DRY-RUN)" if self.dry_run else ""
        user_message = (
            f"Process shipper invoices from the inbox{dry_note}.\n\n"
            f"1. Call list_inbox_emails to get the last {self.config.lookback_days} days of emails.\n"
            f"2. For emails that have PDF attachments and look like invoices "
            f"(subjects containing: invoice, inv, payment request, load, po, "
            f"bill, statement, or sent from carriers/factoring companies), "
            f"call get_email_attachments then extract_invoice_data.\n"
            f"3. Call read_accounts_sheet ONCE.\n"
            f"4. For each extracted invoice:\n"
            f"   - Find the matching row by Load no or Customer Name.\n"
            f"   - Compare Customer Name and Shipper Amt.\n"
            f"   - If match: fill Invoice no, IN received, Payment due date, HST, STATUS=Verified.\n"
            f"   - If discrepancy: fill IN received, write note in Remarks, highlight row RED.\n"
            f"   - If no row found: write the invoice details in Remarks on a new note.\n"
            f"5. Call mark_invoice_processed for each one.\n"
            f"6. Give a final summary table of what was processed."
        )

        messages: list[dict] = [{"role": "user", "content": user_message}]
        logger.info("Starting Phase 1 agent run{}", dry_note)

        while True:
            response = self.client.messages.create(
                model="claude-sonnet-5",
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text"):
                        logger.info("Agent done:\n{}", block.text)
                        return block.text
                return "(Agent finished)"

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    logger.info("→ tool: {}  input: {}", block.name, str(block.input)[:120])
                    result = self._execute_tool(block.name, block.input)
                    logger.debug("  result: {}", result[:200])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
                messages.append({"role": "user", "content": tool_results})
                continue

            logger.warning("Unexpected stop_reason: {}", response.stop_reason)
            return f"Stopped: {response.stop_reason}"
