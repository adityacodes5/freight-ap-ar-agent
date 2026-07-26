"""Copy new loads from the India team's SALES sheet into the ACCOUNTS sheet.

The SALES sheet (separate spreadsheet, maintained by the India team) is the source
of truth for new loads. The ACCOUNTS sheet (the agent's working sheet) needs the
same loads seeded so the invoice agent has rows to match against.

This is a plain copy — no AI. For every load in SALES whose number is higher than
the highest load already in ACCOUNTS, it appends a new ACCOUNTS row carrying only:

    Load no  ←  Load No.
    Customer Name  ←  Client Name
    Shipper Amt  ←  Shipper Rate
    Carrier Amount  ←  Carrier Rate
    Currency  ←  Currency
    Carrier name  ←  Carrier Name

Rows are written into the first empty ACCOUNTS rows (the pre-formatted template
rows), in ascending load order, so the existing formula columns (HST, aging, …)
are left untouched. Idempotent: re-running adds only loads not yet present.

    python sync_sales_to_accounts.py            # dry run — shows what it WOULD add
    python sync_sales_to_accounts.py --commit   # actually writes to ACCOUNTS

The SALES spreadsheet must be shared with the service-account email (read access).
"""
import argparse
import base64
import json
import os
import re
import sys

import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials
from loguru import logger

from config import Config
from tools.sheets import _defang_formula

# CAD loads have their Shipper Amt + Carrier Amount cells highlighted yellow
# (a sheet convention); USD loads stay plain.
_YELLOW = {"backgroundColor": {"red": 1.0, "green": 1.0, "blue": 0.0}}
_CAD = "CAD"

# India-team SALES form. Override via env if it ever moves.
SALES_SPREADSHEET_KEY = os.getenv(
    "SALES_SPREADSHEET_KEY", "1Y50J810TWWjrSC12SbsuXHSVb1126e0VD2sgJ6yUFR4")
SALES_TAB = os.getenv("SALES_SPREADSHEET_TAB", "Sheet1")

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# ACCOUNTS target column  ←  SALES source column   (matched by header text)
COLUMN_MAP = {
    "Load no":       "Load No.",
    "Customer Name": "Client Name",
    "Shipper Amt":   "Shipper Rate",
    "Carrier Amount": "Carrier Rate",
    "Currency":      "Currency",
    "Carrier name":  "Carrier Name",
}
LOAD_HEADER_ACCOUNTS = "Load no"


def _parse_load(value: str) -> int | None:
    """Pull the integer load number out of a cell, or None if there isn't one."""
    m = re.search(r"\d+", str(value or ""))
    return int(m.group()) if m else None


def _header_index(headers: list[str], name: str) -> int:
    """0-based index of a header, raising a clear error if it's missing."""
    try:
        return headers.index(name)
    except ValueError:
        raise RuntimeError(f"Column '{name}' not found. Available: {headers}")


def sync(commit: bool = False) -> dict:
    """Copy new loads from SALES into ACCOUNTS.

    Returns a structured summary so callers (CLI + API/dashboard) can present it:
      {highest_existing, first_empty_row, committed, count, cad_count,
       sales_rows, rows: [{row, load, client, shipper, carrier, currency,
       carrier_name, cad}], message}
    """
    cfg = Config()
    # Base64 service account on hosts with no filesystem (Render), else JSON file.
    if cfg.google_service_account_b64:
        info = json.loads(base64.b64decode(cfg.google_service_account_b64))
        creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
    else:
        creds = Credentials.from_service_account_file(
            cfg.google_service_account_file, scopes=_SCOPES)
    gc = gspread.authorize(creds)

    # ── Source: SALES ───────────────────────────────────────────────────────
    sales_ws = gc.open_by_key(SALES_SPREADSHEET_KEY).worksheet(SALES_TAB)
    sales = sales_ws.get_all_values()
    if not sales:
        raise RuntimeError("SALES sheet is empty.")
    s_head = sales[0]
    s_load = _header_index(s_head, COLUMN_MAP["Load no"])
    s_idx = {acc_col: _header_index(s_head, s_col)
             for acc_col, s_col in COLUMN_MAP.items()}
    logger.info("SALES: {} data rows", len(sales) - 1)

    # ── Target: ACCOUNTS ────────────────────────────────────────────────────
    acc_ss = gc.open_by_key(cfg.google_sheet_id)
    acc_ws = acc_ss.worksheet(cfg.accounts_sheet_name)
    acc = acc_ws.get_all_values()
    if not acc:
        raise RuntimeError("ACCOUNTS sheet is empty.")
    a_head = acc[0]
    a_load = _header_index(a_head, LOAD_HEADER_ACCOUNTS)
    a_cols = {acc_col: _header_index(a_head, acc_col) + 1 for acc_col in COLUMN_MAP}

    existing_loads: set[int] = set()
    max_load = 0
    first_empty_row = None  # 1-based row where Load-no column is blank
    for i, row in enumerate(acc[1:], start=2):  # row 1 is the header
        cell = row[a_load] if a_load < len(row) else ""
        n = _parse_load(cell)
        if n is not None:
            existing_loads.add(n)
            max_load = max(max_load, n)
        elif first_empty_row is None:
            first_empty_row = i
    if first_empty_row is None:
        first_empty_row = len(acc) + 1  # no blank template rows — append at bottom
    logger.info("ACCOUNTS: highest existing load = {}, first empty row = {}",
                max_load, first_empty_row)

    # ── New loads: in SALES, above the ACCOUNTS max, not already present ───────
    new_rows = []
    seen = set()
    for row in sales[1:]:
        load = _parse_load(row[s_load]) if s_load < len(row) else None
        if load is None or load <= max_load or load in existing_loads or load in seen:
            continue
        seen.add(load)
        new_rows.append((load, row))
    new_rows.sort(key=lambda t: t[0])

    result = {
        "highest_existing": max_load,
        "first_empty_row": first_empty_row,
        "sales_rows": len(sales) - 1,
        "committed": False,
        "count": len(new_rows),
        "cad_count": 0,
        "rows": [],
        "message": "",
    }

    if not new_rows:
        result["message"] = f"Nothing to add — ACCOUNTS is already current (max load {max_load})."
        logger.info(result["message"])
        return result

    cells: list[gspread.Cell] = []
    cad_rows: list[int] = []  # rows to highlight yellow (CAD)
    for offset, (load, srow) in enumerate(new_rows):
        target_row = first_empty_row + offset

        def val(acc_col):
            j = s_idx[acc_col]
            return srow[j].strip() if j < len(srow) else ""

        client, ship = val("Customer Name"), val("Shipper Amt")
        carr, cur, cname = val("Carrier Amount"), val("Currency"), val("Carrier name")
        is_cad = cur.strip().upper() == _CAD

        result["rows"].append({
            "row": target_row, "load": load, "client": client, "shipper": ship,
            "carrier": carr, "currency": cur, "carrier_name": cname, "cad": is_cad,
        })
        cells.append(gspread.Cell(target_row, a_cols["Load no"], _defang_formula(str(load))))
        cells.append(gspread.Cell(target_row, a_cols["Customer Name"], _defang_formula(client)))
        cells.append(gspread.Cell(target_row, a_cols["Shipper Amt"], _defang_formula(ship)))
        cells.append(gspread.Cell(target_row, a_cols["Carrier Amount"], _defang_formula(carr)))
        cells.append(gspread.Cell(target_row, a_cols["Currency"], _defang_formula(cur)))
        cells.append(gspread.Cell(target_row, a_cols["Carrier name"], _defang_formula(cname)))
        if is_cad:
            cad_rows.append(target_row)
    result["cad_count"] = len(cad_rows)

    if not commit:
        result["message"] = (f"DRY RUN — {len(new_rows)} load(s) would be added "
                             f"(rows {first_empty_row}-{first_empty_row + len(new_rows) - 1}).")
        logger.info(result["message"])
        return result

    acc_ws.update_cells(cells, value_input_option="USER_ENTERED")
    _highlight_cad(acc_ws, cad_rows, a_cols["Shipper Amt"], a_cols["Carrier Amount"])
    result["committed"] = True
    result["message"] = (f"Wrote {len(new_rows)} load(s) into '{cfg.accounts_sheet_name}' "
                         f"({len(cells)} cells; {len(cad_rows)} CAD highlighted).")
    logger.info("✓ {}", result["message"])
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true",
                    help="actually write to ACCOUNTS (default is a dry run)")
    args = ap.parse_args()

    result = sync(commit=args.commit)
    rows = result["rows"]
    logger.info("ACCOUNTS highest={}, {} new load(s)",
                result["highest_existing"], result["count"])
    if rows:
        print()
        print(f"{'Row':>5}  {'Load':>6}  {'Client':<28}  {'Ship':>9}  {'Carr':>9}  {'Cur':<4}  Carrier")
        print("-" * 110)
        for r in rows:
            print(f"{r['row']:>5}  {r['load']:>6}  {r['client'][:28]:<28}  "
                  f"{r['shipper']:>9}  {r['carrier']:>9}  {r['currency']:<4}  {r['carrier_name']}")
        print()
    logger.info(result["message"])
    if rows and not result["committed"]:
        logger.info("Re-run with --commit to apply.")


def _highlight_cad(ws, rows: list[int], ship_col: int, carr_col: int) -> None:
    """Highlight the Shipper Amt + Carrier Amount cells yellow for the given
    (CAD) rows, in one batched formatting request."""
    if not rows:
        return
    requests = []
    for r in rows:
        requests.append({"range": rowcol_to_a1(r, ship_col), "format": _YELLOW})
        requests.append({"range": rowcol_to_a1(r, carr_col), "format": _YELLOW})
    ws.batch_format(requests)
    logger.info("✓ Highlighted {} CAD row(s) yellow (Shipper Amt + Carrier Amount).",
                len(rows))


if __name__ == "__main__":
    main()
