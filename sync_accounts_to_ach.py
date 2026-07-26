"""Populate the ACH/Check payment sheet from the Accounts sheet.

The ACH/Check sheet is driven entirely by column A (Load No.): every other column
is a VLOOKUP into Accounts, so writing just the load number auto-fills carrier
name, invoice #, factoring co, amount, currency, address, status, etc. This job
finds loads that are DUE TO PAY and appends their load numbers.

Eligibility (a load is "due to pay") — all must hold:
  • a carrier invoice has been captured        (Carrier In# is filled)
  • its Payment due date is on or before the cutoff the operator PICKS on the
    dashboard (plus anything already overdue) — no fixed forward window
  • it isn't already paid or cancelled          (STATUS not Complete / Load Cancelled)
  • its STATUS is NOT "Carrier Payment Issue"   (those are held out — see below)
  • it isn't already on the ACH/Check sheet
Verification is deliberately NOT required — a human does the two-step (dispatch +
manual invoice) check before actually releasing payment.

Batching (we pay weekly, sometimes a few days late after the India team verifies):
  • if the last batch on the sheet is fully PAID (or there's no batch yet) → start
    a NEW batch: a blank separator row, then the new cluster;
  • if the last batch is still open (nothing / only some of it paid) → APPEND to it,
    so re-running mid-week doesn't scatter one week's payments across batches.
Within the new rows, CAD comes first then USD (different rails — EFT vs ACH), and
within each currency the same factoring company / carrier is kept sequential.

Discrepancies vs. Carrier Payment Issues — two DIFFERENT things:
  • A DISCREPANCY (a remark like "invoice vs sheet differ") is still added and paid;
    the row is highlighted MAGENTA and the remark copied into the Notes column
    ("quick pay/Remarks") so the team fixes it at pay time.
  • A "Carrier Payment Issue" (STATUS) is NOT a discrepancy — the carrier side isn't
    clean enough to pay, so the load is HELD OUT of the sheet entirely (never queued)
    and reported in the summary so it's visible.

This runs ONLY from the web dashboard (a manual button) — never on the cron — so
loads aren't added daily while a batch is mid-verification.

    python sync_accounts_to_ach.py            # dry run — shows what it WOULD add
    python sync_accounts_to_ach.py --commit   # actually writes to ACH/Check
"""
import argparse
import datetime as dt
import re

import gspread
from loguru import logger

from config import Config
from tools.sheets import GoogleSheetsClient, _defang_formula

ACH_SHEET = "ACH/Check"

# Accounts statuses we never pay against (already handled).
_SKIP_STATUS = {"complete", "load cancelled"}

# A "Carrier Payment Issue" is NOT a discrepancy — it means the carrier side isn't
# clean enough to pay at all, so these loads are HELD OUT of the ACH sheet entirely
# (never queued for payment). Distinct from a discrepancy, which IS payable.
_HOLD_STATUS = {"carrier payment issue"}

# A row is "flagged" (magenta + remark in Notes) when the Accounts remark reads like
# a DISCREPANCY. Discrepancies are still added and paid — just highlighted so the
# team fixes them at pay time.
_FLAG_REMARK_MARKERS = (
    " vs ", "differ", "extra charge", "not captured",
    "verify total", "verify &", "dispute", "mismatch",
)

# ACH/Check column A drives the row; these columns are VLOOKUPs into Accounts.
# (col_letter, Accounts column index (1-based), range_lookup flag) — matches the
# formulas already in the sheet.
_LOOKUP_COLS = [
    ("B", 11, "true"),   # Carrier name
    ("C", 12, "true"),   # Carrier Inv. No
    ("D", 13, "true"),   # Fact.Co. (yes/no)
    ("E", 14, "true"),   # Invoice rec
    ("F", 15, "true"),   # Due date
    ("G", 16, "true"),   # Amount
    ("H", 17, "false"),  # currency (exact match)
    ("K", 24, "true"),   # Address (factoring co + address)
    ("L", 20, "true"),   # STATUS
    ("M", 23, "true"),   # QP Margin
]
_NOTES_COL = 14      # col N — "quick pay/Remarks"
_LOAD_COL = 1        # col A
_CURRENCY_COL = "H"  # currency cell — highlighted yellow for CAD (sheet convention)
_YELLOW = {"backgroundColor": {"red": 1.0, "green": 1.0, "blue": 0.0}}


def _col_to_idx(letter: str) -> int:
    return ord(letter) - ord("A") + 1


def _parse_date(s: str):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _load_int(value: str):
    m = re.search(r"\d+", str(value or ""))
    return int(m.group()) if m else None


def _is_flagged(remark: str) -> bool:
    """True if the Accounts remark reads like a discrepancy → add but highlight
    magenta. (Carrier Payment Issues are held out earlier and never reach here.)"""
    rl = (remark or "").lower()
    return any(m in rl for m in _FLAG_REMARK_MARKERS)


def sync_ach(commit: bool = False, through: "str | dt.date | None" = None,
             today: dt.date | None = None) -> dict:
    """Append due-to-pay loads to the ACH/Check sheet. Returns a structured summary
    so the CLI, API/dashboard, and daily email can all present it.

    `through` = the cutoff the operator picks on the dashboard: pay every load due
    on or before this date (plus anything already overdue). Accepts a date or an
    ISO / MM-DD-YYYY string. Defaults to today (overdue + due-today only) if omitted.
    """
    cfg = Config()
    gc = GoogleSheetsClient(cfg)
    today = today or dt.date.today()
    # Manual cutoff: pay loads due on or before the picked date (overdue included,
    # since their due dates are < today <= horizon). No more fixed forward window.
    if through is None:
        horizon = today
    elif isinstance(through, dt.date):
        horizon = through
    else:
        horizon = _parse_date(through) or today

    acc = gc.get_all_values(cfg.accounts_sheet_name)
    if not acc:
        raise RuntimeError("Accounts sheet is empty.")
    h = acc[0]

    def idx(name: str) -> int:
        return h.index(name)

    i_load = idx("Load no")
    i_cin = idx("Carrier In#")
    i_due = idx("Payment due date ")
    i_status = idx("STATUS")
    i_cur = idx("Currency")
    i_amt = idx("Carrier Amount")
    i_carrier = idx("Carrier name")
    i_remark = idx("Remarks")
    # Payee grouping: the factoring company lives in the SECOND "Factoring" column.
    fact_cols = [i for i, x in enumerate(h) if x.strip() == "Factoring"]
    i_factco = fact_cols[-1] if fact_cols else i_carrier

    # Load numbers already on the ACH/Check sheet (skip duplicates), plus the row
    # of the last load so we know where to write.
    ach = gc.get_all_values(ACH_SHEET)
    in_ach: set[int] = set()
    last_load_row = 1
    for ri, row in enumerate(ach[1:], start=2):
        n = _load_int(row[0]) if row else None
        if n is not None:
            in_ach.add(n)
            last_load_row = ri

    # ── Which batch do these loads join? ─────────────────────────────────────
    # We pay in weekly batches separated by a blank row. Rule (per the team):
    #   • last batch is fully PAID (or no batch yet) → cut the line, start a NEW
    #     batch (a blank separator row, then the new cluster);
    #   • last batch is still open (nothing / only some paid) → APPEND to it, so a
    #     mid-week re-run doesn't scatter the same week's payments across batches.
    # "Paid/sent" = the ACH row has a 'date mailed' OR its STATUS is Complete.
    ACH_DATE_MAILED, ACH_STATUS = 9, 11  # 0-based cols J and L
    last_batch_rows = []
    r = last_load_row
    while r >= 2 and _load_int(ach[r - 1][0] if r - 1 < len(ach) else "") is not None:
        last_batch_rows.append(ach[r - 1])
        r -= 1

    def _settled(row) -> bool:
        # A batch row is "settled" (payment handled) if it has a date-mailed value
        # (incl. manual notes like "vc received") OR a STATUS that reads as sent/paid
        # ("Complete", "email sent", "paid", "cleared"). Freshly auto-added rows have
        # neither (their STATUS pulls "Verified"/blank from Accounts), so a new,
        # untouched batch stays OPEN until the team actually processes it.
        mailed = (row[ACH_DATE_MAILED] if ACH_DATE_MAILED < len(row) else "").strip()
        status = (row[ACH_STATUS] if ACH_STATUS < len(row) else "").strip().lower()
        return bool(mailed) or any(k in status for k in ("complete", "sent", "paid", "cleared"))

    last_batch_all_paid = bool(last_batch_rows) and all(_settled(x) for x in last_batch_rows)
    open_batch = bool(last_batch_rows) and not last_batch_all_paid

    # ── Select due-to-pay loads ──────────────────────────────────────────────
    due = []
    held = []  # Carrier Payment Issue loads that were due but held out of payment
    for row in acc[1:]:
        load = _load_int(row[i_load]) if i_load < len(row) else None
        if load is None:
            continue
        cin = (row[i_cin] if i_cin < len(row) else "").strip()
        if not cin:
            continue  # no carrier invoice captured yet
        status = (row[i_status] if i_status < len(row) else "").strip()
        if status.lower() in _SKIP_STATUS:
            continue
        d = _parse_date(row[i_due] if i_due < len(row) else "")
        if d is None or d > horizon:
            continue  # not due within the window (and not overdue)
        if load in in_ach:
            continue  # already queued/paid on the ACH sheet
        if status.lower() in _HOLD_STATUS:
            held.append(load)     # Carrier Payment Issue → never queue for payment
            continue

        cur = (row[i_cur] if i_cur < len(row) else "").strip().upper()
        amt = (row[i_amt] if i_amt < len(row) else "").strip()
        carrier = (row[i_carrier] if i_carrier < len(row) else "").strip()
        factco = (row[i_factco] if i_factco < len(row) else "").strip()
        remark = (row[i_remark] if i_remark < len(row) else "").strip()
        payee = factco or carrier
        flagged = _is_flagged(remark)
        due.append({
            "load": load, "currency": cur, "amount": amt, "carrier": carrier,
            "payee": payee, "status": status, "remark": remark, "due": d.isoformat(),
            "flagged": flagged,
        })

    # ── Order: CAD cluster first, then USD; same payee sequential within each ──
    def sort_key(r):
        return (r["payee"].lower(), r["load"])

    cad = sorted([r for r in due if r["currency"] == "CAD"], key=sort_key)
    usd = sorted([r for r in due if r["currency"] == "USD"], key=sort_key)
    other = sorted([r for r in due if r["currency"] not in ("CAD", "USD")], key=sort_key)
    ordered = cad + usd + other

    # Append into the open batch (no gap), or start a new batch (one blank row).
    if open_batch:
        start_row = last_load_row + 1
        batch_action = "appended to the current open batch"
    else:
        start_row = last_load_row + 2 if last_load_row > 1 else 3
        batch_action = ("started a new batch (last one is fully paid)"
                        if last_batch_rows else "started the first batch")
    for offset, r in enumerate(ordered):
        r["ach_row"] = start_row + offset

    result = {
        "count": len(ordered),
        "cad_count": len(cad),
        "usd_count": len(usd),
        "flagged_count": sum(1 for r in ordered if r["flagged"]),
        "held_count": len(held),
        "held_loads": held,
        "committed": False,
        "start_row": start_row,
        "batch_action": batch_action,
        "window_through": horizon.isoformat(),
        "rows": ordered,
        "message": "",
    }

    held_note = (f" Held out {len(held)} Carrier Payment Issue load(s) "
                 f"({', '.join(str(x) for x in held)}) — not queued for payment."
                 if held else "")

    if not ordered:
        result["message"] = (f"Nothing due to pay on or before {horizon:%b %d} — "
                             "ACH/Check is already current." + held_note)
        logger.info(result["message"])
        return result

    end_row = start_row + len(ordered) - 1
    if not commit:
        result["message"] = (
            f"DRY RUN — {len(ordered)} load(s) due on/before {horizon:%b %d} would be "
            f"{batch_action} (rows {start_row}-{end_row}; {len(cad)} CAD, {len(usd)} USD, "
            f"{result['flagged_count']} flagged magenta)." + held_note)
        logger.info(result["message"])
        return result

    # ── Write: col A (load) + the VLOOKUP formulas + Notes for flagged rows ────
    ws = gc._ws(ACH_SHEET)
    cells: list[gspread.Cell] = []
    magenta_rows: list[int] = []
    cad_rows: list[int] = []
    for r in ordered:
        row_n = r["ach_row"]
        cells.append(gspread.Cell(row_n, _LOAD_COL, _defang_formula(str(r["load"]))))
        for letter, acc_col, rl in _LOOKUP_COLS:
            formula = f"=VLOOKUP(A{row_n},Accounts!A:X,{acc_col},{rl})"
            cells.append(gspread.Cell(row_n, _col_to_idx(letter), formula))
        if r["flagged"]:
            note = r["remark"] or r["status"]
            cells.append(gspread.Cell(row_n, _NOTES_COL, _defang_formula(note)))
            magenta_rows.append(row_n)
        if r["currency"] == "CAD":
            cad_rows.append(row_n)

    ws.update_cells(cells, value_input_option="USER_ENTERED")
    # CAD convention: the currency cell (col H) is highlighted yellow. Do this BEFORE
    # magenta so a flagged CAD row still shows the magenta flag over the whole row.
    if cad_rows:
        ws.batch_format([{"range": f"{_CURRENCY_COL}{r}", "format": _YELLOW} for r in cad_rows])
    for row_n in magenta_rows:
        gc.highlight_row_magenta(ACH_SHEET, row_n)

    result["committed"] = True
    result["message"] = (
        f"Wrote {len(ordered)} load(s) to ACH/Check — {batch_action} "
        f"(rows {start_row}-{end_row}; {len(cad)} CAD, {len(usd)} USD, "
        f"{len(magenta_rows)} flagged magenta)." + held_note)
    logger.info("✓ {}", result["message"])
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true",
                    help="actually write to ACH/Check (default is a dry run)")
    ap.add_argument("--through", type=str, default=None,
                    help="pay loads due on or before this date (plus overdue), "
                         "e.g. 2026-07-20. Defaults to today.")
    args = ap.parse_args()

    res = sync_ach(commit=args.commit, through=args.through)
    rows = res["rows"]
    if rows:
        print()
        print(f"Batch decision : {res['batch_action']}")
        print(f"Writing rows   : {res['start_row']}-{res['start_row'] + len(rows) - 1} "
              f"(only col A + formulas; magenta rows also get a Note)")
        print(f"Due window     : on/before {res['window_through']}\n")
        print(f"{'Row':>5}  {'Load':>6}  {'Cur':<4}  {'Amt':>8}  {'Payee':<26}  Flag / Note")
        print("-" * 92)
        for r in rows:
            note = f"MAGENTA — {r['remark'][:30]}" if r["flagged"] else ""
            print(f"{r['ach_row']:>5}  {r['load']:>6}  {r['currency']:<4}  "
                  f"{r['amount']:>8}  {r['payee'][:26]:<26}  {note}")
        print()
    logger.info(res["message"])
    if rows and not res["committed"]:
        logger.info("Re-run with --commit to apply.")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    main()
