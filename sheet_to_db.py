"""
Sheet → DB migration: read the DEMO Accounts 2026 sheet (READ-ONLY) and land it
in project 2's Postgres.

Pipeline (raw landing zone → curated model):

    1.  --report      Read the sheet and print a data-quality report. No writes.
    2.  --load-raw    Truncate + reload `sheet_accounts_raw` with every sheet row,
                      verbatim (text), plus a jsonb copy. Additive: touches only
                      the staging table, nothing in the relational model.

    (promotion of staging → companies/loads/invoices/payments lives in
     promote_sheet.py, run after the dummy data is wiped.)

The sheet stays READ-ONLY — this module never writes to Google Sheets.

    python sheet_to_db.py --report
    python sheet_to_db.py --load-raw
"""
import argparse
import json
import re
from collections import Counter
from datetime import datetime

from loguru import logger

from config import Config
from tools.sheets import GoogleSheetsClient

# ── Canonical column map ──────────────────────────────────────────────────────
# Sheet headers are messy (stray spaces, duplicate "Factoring"/"Remarks",
# summary columns mixed in). We match on a normalized key (lowercase, alnum only)
# so " client Cheque no" and "Payment due date " resolve cleanly, and we keep
# ONLY real per-load columns — the trailing aggregate/aging columns are dropped.
_CANON = {
    "loadno": "load_no",
    "invoiceno": "invoice_no",
    "shippo": "ship_po",                 # "ship. P.O. Number"
    "shipponumber": "ship_po",
    "customername": "customer_name",
    "invoicedate": "invoice_date",
    "shipperamt": "shipper_amt",
    "hst": "hst",
    "duedate": "due_date",
    "clientchequeno": "client_cheque_no",
    "pmtrevd": "pmt_revd",
    "carriername": "carrier_name",
    "carrierin": "carrier_in_no",        # "Carrier In#"
    "factoring": "factoring",
    "factoring2": "factoring2",
    "inreceived": "in_received",
    "paymentduedate": "payment_due_date",
    "carrieramount": "carrier_amount",
    "currency": "currency",
    "checkno": "check_no",
    "taxid": "tax_id",
    "status": "status",
    "margin": "margin",
    "qpamount": "qp_amount",
    "qpdifference": "qp_difference",
    "remarks": "remarks",
    "remarks2": "remarks2",
}

# Every canonical field, in a stable order (drives the staging table columns).
RAW_COLUMNS = [
    "load_no", "invoice_no", "ship_po", "customer_name", "invoice_date",
    "shipper_amt", "hst", "due_date", "client_cheque_no", "pmt_revd",
    "carrier_name", "carrier_in_no", "factoring", "factoring2", "in_received",
    "payment_due_date", "carrier_amount", "currency", "check_no", "tax_id",
    "status", "margin", "qp_amount", "qp_difference", "remarks", "remarks2",
]


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (header or "").lower())


def read_records(cfg: Config | None = None) -> list[dict]:
    """Return one dict per sheet data row: {canonical_field: text, ..., sheet_row}.
    Read-only. Only rows carrying a numeric load number are returned."""
    cfg = cfg or Config()
    gsc = GoogleSheetsClient(cfg)
    records = gsc.get_all_records(cfg.accounts_sheet_name)  # dedups dup headers → _2

    out = []
    for i, rec in enumerate(records):
        row_no = i + 2  # row 1 = header
        canon = {c: "" for c in RAW_COLUMNS}
        for header, value in rec.items():
            key = _CANON.get(_norm(header))
            if key:
                canon[key] = str(value).strip() if value is not None else ""
        # keep only rows with a real load number
        if not re.search(r"\d", canon["load_no"]):
            continue
        canon["sheet_row"] = row_no
        out.append(canon)
    logger.info("Read {} load rows from '{}'", len(out), cfg.accounts_sheet_name)
    return out


# ── Parsing helpers (used by the report + later promotion) ─────────────────────

def clean_number(v: str):
    """'3,704.40' → 3704.4 ; 'EFT' / '' → None."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("$", "").replace(" ", "")
    if s in ("", "-", "—"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        n = float(s)
        return -n if neg else n
    except ValueError:
        return None


_DATE_FMTS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y")


def parse_date(v: str):
    """Return ISO 'YYYY-MM-DD' or None. None for blanks; raises nothing."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ── Report ────────────────────────────────────────────────────────────────────

def report(cfg: Config | None = None) -> None:
    rows = read_records(cfg)
    n = len(rows)
    print(f"\n{'='*60}\n  ACCOUNTS sheet — data-quality report\n{'='*60}")
    print(f"Load rows (numeric load no):      {n}")

    # duplicate load numbers
    loads = [r["load_no"] for r in rows]
    dups = [k for k, c in Counter(loads).items() if c > 1]
    print(f"Duplicate load numbers:           {len(dups)}"
          + (f"  e.g. {dups[:8]}" if dups else ""))

    # currency
    cur = Counter((r["currency"] or "∅blank").upper() for r in rows)
    print(f"Currency:                         {dict(cur)}")

    def _miss(field):
        return sum(1 for r in rows if not r[field].strip())

    def _badnum(field):
        return sum(1 for r in rows if r[field].strip() and clean_number(r[field]) is None)

    def _baddate(field):
        return sum(1 for r in rows if r[field].strip() and parse_date(r[field]) is None)

    print("\n-- missing / unparseable (per field) --")
    for f in ("customer_name", "carrier_name", "shipper_amt", "carrier_amount",
              "currency", "invoice_no"):
        print(f"  missing {f:18} {_miss(f):4}")
    for f in ("shipper_amt", "carrier_amount", "hst", "margin"):
        print(f"  non-numeric {f:15} {_badnum(f):4}")
    for f in ("invoice_date", "in_received", "pmt_revd", "due_date"):
        print(f"  unparseable date {f:12} {_baddate(f):4}")

    # presence of invoice/payment signals
    have_carrier_inv = sum(1 for r in rows if r["in_received"].strip() or r["carrier_in_no"].strip())
    have_shipper_inv = sum(1 for r in rows if r["invoice_date"].strip() or r["invoice_no"].strip())
    have_pmt = sum(1 for r in rows if r["pmt_revd"].strip())
    have_factoring = sum(1 for r in rows if r["factoring"].strip() or r["factoring2"].strip())
    print("\n-- signals --")
    print(f"  rows w/ carrier invoice (IN recd/Carrier In#): {have_carrier_inv}")
    print(f"  rows w/ shipper invoice (date/Invoice no):     {have_shipper_inv}")
    print(f"  rows w/ shipper payment received (PMT Revd):    {have_pmt}")
    print(f"  rows w/ factoring company:                     {have_factoring}")

    # STATUS + Remarks distributions
    print("\n-- STATUS values --")
    for val, c in Counter((r["status"].strip() or "∅blank") for r in rows).most_common(20):
        print(f"  {c:4}  {val}")
    print("\n-- top Remarks --")
    for val, c in Counter((r["remarks"].strip() or "∅blank") for r in rows).most_common(15):
        print(f"  {c:4}  {val[:60]}")

    # distinct company name counts (how many companies we'll create)
    shippers = {r["customer_name"].strip() for r in rows if r["customer_name"].strip()}
    carriers = {r["carrier_name"].strip() for r in rows if r["carrier_name"].strip()}
    print("\n-- distinct names (→ companies) --")
    print(f"  distinct shippers (customers): {len(shippers)}")
    print(f"  distinct carriers:             {len(carriers)}")
    print(f"{'='*60}\n")


# ── Raw staging load ──────────────────────────────────────────────────────────

def load_raw(cfg: Config | None = None) -> dict:
    """Truncate + reload sheet_accounts_raw with every load row (text + jsonb).
    Only touches the staging table."""
    from db import USE_DB, get_conn
    if not USE_DB:
        raise RuntimeError("DATABASE_URL not set — cannot load staging")
    rows = read_records(cfg)

    cols = RAW_COLUMNS + ["sheet_row", "raw"]
    placeholders = ", ".join(["%s"] * len(cols))
    collist = ", ".join(cols)

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("truncate table sheet_accounts_raw")
        for r in rows:
            raw_json = {k: r.get(k, "") for k in RAW_COLUMNS}
            vals = [r.get(c, "") for c in RAW_COLUMNS] + [r["sheet_row"], json.dumps(raw_json)]
            cur.execute(
                f"insert into sheet_accounts_raw ({collist}) values ({placeholders})",
                vals,
            )
    logger.info("Loaded {} rows into sheet_accounts_raw", len(rows))
    return {"loaded": len(rows)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Sheet → DB migration (read-only sheet)")
    ap.add_argument("--report", action="store_true", help="print data-quality report (no writes)")
    ap.add_argument("--load-raw", action="store_true", help="reload sheet_accounts_raw staging table")
    args = ap.parse_args()

    if args.load_raw:
        print(load_raw())
    else:
        report()
