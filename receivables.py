"""Cash application — record customer payments (a cheque split across loads) onto
the "Payment Received" tab.

That tab is the single source of truth for AR: writing a row there (keyed by the
load / invoice number in col A) makes Accounts' PMT Revd + client Cheque no
auto-populate via VLOOKUP, which marks the load paid (drops it off Overdue) and
feeds the inflow analytics. So we only ever write to "Payment Received".

The tab is pre-built with formula rows below the real data: col A (Invoice) is
blank, but Amount/HST/TOTAL/Currency/Shipper (cols B,C,D,E,H) are VLOOKUP formulas
keyed on col A. The native way to record a payment is therefore to fill only col A
(Invoice), F (Check/method) and G (Date) on the FIRST FREE LINE — right after the
last real entry — and let the formulas populate the rest. We do exactly that; only
a split/partial payment (amount ≠ the load's full invoice) overrides the amount
columns with literals, because the VLOOKUP would otherwise show the full invoice.
"""
import datetime as dt
import re

import gspread

from config import Config
from tools.sheets import GoogleSheetsClient
from analytics import _num, _date

PR_TAB = "Payment Received"
# Columns (1-indexed) on the Payment Received tab.
C_INVOICE, C_AMOUNT, C_HST, C_TOTAL, C_CUR, C_CHECK, C_DATE, C_SHIPPER, C_REMARK = range(1, 10)


def _is_break_row(bg: dict) -> bool:
    """True if a cell background is a coloured month-break separator (not white).
    Google omits zero channels, so a cyan break comes back as {green:1, blue:1}
    (red absent → 0); a real/empty row is white {1,1,1} or an empty dict."""
    if not bg:
        return False
    return not all(bg.get(ch, 0) >= 0.9 for ch in ("red", "green", "blue"))


def _first_free_line(gc, first_row: int, window: int = 80) -> int:
    """The first writable line at or below `first_row`, skipping blue month-break
    separator rows (which are blank in col A but highlighted, e.g. a 'new month'
    divider). Falls back to first_row if nothing readable."""
    bgs = gc.column_backgrounds(PR_TAB, "A", first_row, first_row + window - 1)
    for i, bg in enumerate(bgs):
        if not _is_break_row(bg):
            return first_row + i
    return first_row + len(bgs) if bgs else first_row


def index() -> dict:
    """Map every load with a Shipper Amt → its expected invoice figures, so the UI
    can show the correct amount beside each row and pre-fill the split."""
    gc = GoogleSheetsClient(Config())
    acc = gc.get_all_values("Accounts")
    h = acc[0]

    def i(name):
        return h.index(name)

    i_load, i_inv, i_cust, i_sa, i_hst, i_cur, i_pmt = (
        i("Load no"), i("Invoice no"), i("Customer Name"), i("Shipper Amt"),
        i("HST"), i("Currency"), i("PMT Revd"))

    loads: dict[str, dict] = {}
    for row in acc[1:]:
        raw = (row[i_load] if i_load < len(row) else "").strip()
        m = re.search(r"\d+", raw)
        if not m:
            continue
        load = m.group()
        shipper = _num(row[i_sa]) if i_sa < len(row) else 0
        if shipper <= 0:
            continue
        hst = _num(row[i_hst]) if i_hst < len(row) else 0
        cur = (row[i_cur] if i_cur < len(row) else "").strip().upper()
        inv = (row[i_inv] if i_inv < len(row) else "").strip() or load
        paid = _date(row[i_pmt] if i_pmt < len(row) else "") is not None
        loads[load] = {
            "invoice": re.sub(r"\s", "", inv),
            "customer": (row[i_cust] if i_cust < len(row) else "").strip(),
            "currency": cur if cur in ("CAD", "USD") else "",
            "shipper_amt": round(shipper, 2),
            "hst": round(hst, 2),
            "expected": round(shipper + hst, 2),
            "paid": paid,
        }
    return {"loads": loads, "count": len(loads)}


def apply(date: str, method: str, lines: list[dict],
          customer: str = "", reference: str = "") -> dict:
    """Record one Payment Received row per load line at the first free line (right
    after the last real entry), preserving the sheet's VLOOKUP formulas. `lines` =
    [{load, amount}]. Returns a summary. Writes nothing if `lines` is empty."""
    if not date or not str(date).strip():
        raise ValueError("A payment date is required.")
    if not lines:
        raise ValueError("No loads to record.")

    gc = GoogleSheetsClient(Config())
    idx = index()["loads"]
    # Match the historical "Payment Received" format the team used by hand:
    #   • col F ("Check") = the METHOD WORD only — EFT / ACH / Cheque / Wire.
    #     Never a number, and never a currency suffix (currency has its own column,
    #     E, via VLOOKUP), so "Cheque-CAD"/"Cheque-USD" collapse to "Cheque".
    #   • Remarks: cheque payments note the cheque number ("Cheque 12345 split");
    #     EFT/ACH/Wire leave it blank, the way the team records them.
    check = (method or "").split("-")[0].strip() or "EFT"
    is_cheque = check.lower().startswith("cheque")
    remark = f"Cheque {reference} split" if (is_cheque and reference) else ""

    ws = gc._ws(PR_TAB)
    # First free line = the row after the last non-empty Invoice cell (col A) —
    # NOT the very bottom past the trailing formula rows — then skip any blue
    # month-break separator rows so we land on the first real writable line.
    col_a = ws.col_values(C_INVOICE)
    start_row = _first_free_line(gc, len(col_a) + 1)
    grid = ws.get(value_render_option="FORMULA")

    def has_formula(row: int, col: int) -> bool:
        r = grid[row - 1] if row - 1 < len(grid) else []
        return col - 1 < len(r) and str(r[col - 1]).lstrip().startswith("=")

    cells: list[gspread.Cell] = []
    applied: list[dict] = []
    total = 0.0
    for offset, ln in enumerate(lines):
        load = str(ln.get("load", "")).strip()
        m = re.search(r"\d+", load)
        if not m:
            raise ValueError(f"Bad load number: {load!r}")
        load = m.group()
        amount = round(float(ln.get("amount") or 0), 2)
        if amount <= 0:
            raise ValueError(f"Load {load}: amount must be positive.")
        info = idx.get(load, {})
        invoice = info.get("invoice") or load
        cur = (ln.get("currency") or info.get("currency") or "").upper()
        cust = customer or info.get("customer", "")
        matched = bool(info) and abs(amount - info.get("expected", -1)) < 0.01

        r = start_row + offset
        # Always literal: Invoice, Check/method, Date, Remark.
        cells += [gspread.Cell(r, C_INVOICE, invoice), gspread.Cell(r, C_CHECK, check),
                  gspread.Cell(r, C_DATE, date), gspread.Cell(r, C_REMARK, remark)]
        # Amount/HST/TOTAL: keep the VLOOKUP formulas for a full payment on a
        # template row; write literals for a split/partial, or if we've run past
        # the formula block (so the amount is never left blank).
        templated = has_formula(r, C_AMOUNT)
        if not matched or not templated:
            base = info["shipper_amt"] if matched else amount
            hst = info["hst"] if matched else ""
            cells += [gspread.Cell(r, C_AMOUNT, base), gspread.Cell(r, C_HST, hst),
                      gspread.Cell(r, C_TOTAL, amount)]
            if not templated:  # no formulas here → fill currency + shipper too
                cells += [gspread.Cell(r, C_CUR, cur), gspread.Cell(r, C_SHIPPER, cust)]

        applied.append({"load": load, "invoice": invoice, "amount": amount,
                        "currency": cur, "expected": info.get("expected"),
                        "matched": matched, "already_paid": info.get("paid", False),
                        "row": r})
        total += amount

    ws.update_cells(cells, value_input_option="USER_ENTERED")

    return {
        "written": len(lines),
        "total": round(total, 2),
        "first_row": start_row,
        "rows": [a["row"] for a in applied],
        "applied": applied,
        "message": f"Recorded {len(lines)} payment(s) totaling {total:g} at row {start_row} of Payment Received.",
    }


def _template_cells(r: int) -> list:
    """The blank formula-template cells for a Payment Received row — used to REVERSE
    a recorded payment: clear A/F/G/I and restore the VLOOKUP formulas so Accounts'
    PMT Revd reverts to #N/A (load back to unpaid)."""
    return [
        gspread.Cell(r, C_INVOICE, ""),
        gspread.Cell(r, C_AMOUNT, f"=VLOOKUP(A{r},Accounts!B:J,5,false)"),
        gspread.Cell(r, C_HST, f"=VLOOKUP(A{r},Accounts!B:J,6,false)"),
        gspread.Cell(r, C_TOTAL, f"=B{r}+C{r}"),
        gspread.Cell(r, C_CUR, f"=vlookup(A{r},Accounts!$A$1:$AH$10996,17,false)"),
        gspread.Cell(r, C_CHECK, ""),
        gspread.Cell(r, C_DATE, ""),
        gspread.Cell(r, C_SHIPPER, f"=VLOOKUP(A{r},Accounts!B:J,3,false)"),
        gspread.Cell(r, C_REMARK, ""),
    ]


def undo(rows: list[int]) -> dict:
    """Reverse a just-recorded payment by resetting its row(s) to the blank formula
    template. `rows` = the row numbers from a prior apply() result."""
    rows = [int(r) for r in (rows or []) if str(r).strip()]
    if not rows:
        raise ValueError("No rows to undo.")
    gc = GoogleSheetsClient(Config())
    ws = gc._ws(PR_TAB)
    cells: list = []
    for r in rows:
        cells += _template_cells(r)
    ws.update_cells(cells, value_input_option="USER_ENTERED")
    return {"cleared": len(rows), "rows": rows,
            "message": f"Reversed {len(rows)} payment row(s) — loads are unpaid again."}
