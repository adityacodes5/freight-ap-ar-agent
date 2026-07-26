"""
Promote the raw sheet landing zone (sheet_accounts_raw) into project 2's
relational model: companies → loads → carrier_invoices / shipper_invoices →
payments_received + payment_allocations.

    python promote_sheet.py --dry-run   # compute + print the plan, no writes
    python promote_sheet.py --commit    # WIPE business tables, then load for real

--commit truncates ONLY the business tables (loads, invoices, payments,
companies, company_locations, documents, audit_log) and rebuilds them from
staging in one transaction. It never touches:
  • the agent_* cache/state tables,
  • the users table (real admin + the automation user),
  • exchange_rates / auth tables / schema_migrations / sheet_accounts_raw.

Everything is attributed to the `automation` system user (created_by).

Column semantics learned from the sheet:
  • factoring  = 'yes'/'no' flag;  factoring2 = factoring company + address blob
    (only a real factoring company when the flag is 'yes').
  • PMT Revd / client Cheque no = '#N/A'  → shipper hasn't paid yet.
  • A carrier invoice exists only when 'IN received' is a real date;
    a shipper invoice only when 'invoice date' is a real date.
"""
import argparse
import re
from collections import Counter
from datetime import date

from dotenv import load_dotenv

load_dotenv()  # must run before importing db (which reads DATABASE_URL at import)

from loguru import logger  # noqa: E402

from db import USE_DB, DATABASE_URL  # noqa: E402
from sheet_to_db import RAW_COLUMNS, clean_number, parse_date  # noqa: E402

AUTOMATION_USER_ID = 2  # users.user_id for automation@demo.internal

# Wipe order is irrelevant with CASCADE, but list every business table explicitly.
BUSINESS_TABLES = [
    "payment_allocations", "payments_received", "carrier_invoices",
    "shipper_invoices", "documents", "company_locations", "loads",
    "companies", "audit_log",
]

_COLS = RAW_COLUMNS + ["sheet_row"]


# ── derivations ───────────────────────────────────────────────────────────────

def norm_factoring_name(blob: str) -> str:
    """'RTS International Inc, C/o911820 PO BOX...' → 'RTS International Inc'."""
    s = re.split(r"\d", blob or "", 1)[0]
    s = re.sub(r"[,/]\s*c\s*/?\s*o\s*$", "", s.strip(), flags=re.I)  # trailing C/o
    return s.strip(" ,.-/")


def method_for(cheque: str, currency: str):
    c = (cheque or "").strip().upper()
    if c == "EFT":
        return "EFT-CAD" if currency == "CAD" else "EFT-USD"
    if c == "ACH":
        return "ACH-USD"
    return None  # INTERAC / cheque # / #N/A / blank → leave null


def _flag(s: str) -> bool:
    return (s or "").strip().lower() in ("yes", "y", "true", "1")


def derive_carrier_review(status: str, notes: str):
    """→ (review_status, has_discrepancy, issue_note)."""
    st, low = (status or "").lower(), (notes or "").lower()
    issue_words = ("issue", "hold", "mismatch", "dispute", " vs ", "higher",
                   "short", "discrepan")
    if "carrier payment issue" in st or any(w in low for w in issue_words):
        return "issue", True, (notes.strip() or None)
    if "✓" in (notes or "") or "verified" in low or "verified" in st:
        return "checked", False, None
    return "unchecked", False, None


def load_staging(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(f"select {', '.join(_COLS)} from sheet_accounts_raw order by sheet_row")
        return [dict(zip(_COLS, r)) for r in cur.fetchall()]


def build_plan(rows: list[dict]) -> dict:
    """Compute what would be created (no DB writes). Returns counts + samples."""
    shippers, carriers, factors = set(), set(), set()
    n_loads = n_carrier_inv = n_shipper_inv = n_payments = 0
    skip_carrier = skip_shipper = 0
    status_ct, cur_ct = Counter(), Counter()
    samples = []

    for r in rows:
        cust = (r["customer_name"] or "").strip()
        carr = (r["carrier_name"] or "").strip()
        currency = (r["currency"] or "").strip().upper()
        if cust:
            shippers.add(cust)
        if carr:
            carriers.add(carr)
        if _flag(r["factoring"]) and (r["factoring2"] or "").strip():
            factors.add(norm_factoring_name(r["factoring2"]))

        in_recd = parse_date(r["in_received"])
        sent = parse_date(r["invoice_date"])
        pmt = parse_date(r["pmt_revd"])
        has_carrier_doc = bool((r["in_received"] or "").strip() or (r["carrier_in_no"] or "").strip())
        has_shipper_doc = bool((r["invoice_date"] or "").strip() or (r["invoice_no"] or "").strip())

        n_loads += 1
        cur_ct[currency] += 1
        if in_recd:
            n_carrier_inv += 1
        elif has_carrier_doc:
            skip_carrier += 1
        if sent:
            n_shipper_inv += 1
        elif has_shipper_doc:
            skip_shipper += 1
        if pmt and sent:
            n_payments += 1

        st = "cancelled" if "cancel" in (r["status"] or "").lower() else (
            "paid" if (pmt or (r["status"] or "").lower() == "complete") else (
                "invoiced" if has_shipper_doc else "confirmed"))
        status_ct[st] += 1
        if len(samples) < 6:
            samples.append({
                "load": r["load_no"], "cur": currency, "ship": cust[:18],
                "carr": carr[:18], "s_amt": clean_number(r["shipper_amt"]),
                "c_amt": clean_number(r["carrier_amount"]), "status": st,
                "carrier_inv": bool(in_recd), "shipper_inv": bool(sent),
                "paid": bool(pmt)})

    return {
        "loads": n_loads, "companies_shipper": len(shippers),
        "companies_carrier": len(carriers), "companies_factoring": len(factors),
        "carrier_invoices": n_carrier_inv, "shipper_invoices": n_shipper_inv,
        "payments": n_payments, "skipped_carrier_inv_no_date": skip_carrier,
        "skipped_shipper_inv_no_date": skip_shipper,
        "load_status": dict(status_ct), "currency": dict(cur_ct),
        "samples": samples,
    }


def print_plan(plan: dict) -> None:
    print(f"\n{'='*60}\n  PROMOTION PLAN (staging → relational)\n{'='*60}")
    print(f"  companies: {plan['companies_shipper']} shippers + "
          f"{plan['companies_carrier']} carriers + "
          f"{plan['companies_factoring']} factoring")
    print(f"  loads:            {plan['loads']}   {plan['currency']}")
    print(f"  carrier_invoices: {plan['carrier_invoices']}  "
          f"(skipped, no IN-received date: {plan['skipped_carrier_inv_no_date']})")
    print(f"  shipper_invoices: {plan['shipper_invoices']}  "
          f"(skipped, no invoice date: {plan['skipped_shipper_inv_no_date']})")
    print(f"  payments_received:{plan['payments']}")
    print(f"  load status: {plan['load_status']}")
    print("  sample loads:")
    for s in plan["samples"]:
        print(f"    {s}")
    print(f"{'='*60}\n")


# ── commit ────────────────────────────────────────────────────────────────────

def promote(rows: list[dict]) -> dict:
    """WIPE business tables and rebuild from staging in one transaction."""
    import psycopg
    conn = psycopg.connect(DATABASE_URL, autocommit=False, connect_timeout=15)
    created = Counter()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "truncate table " + ", ".join(BUSINESS_TABLES) + " restart identity cascade")

            company_id: dict[tuple, int] = {}

            def get_company(name: str, ctype: str, **extra) -> int | None:
                name = (name or "").strip()
                if not name:
                    return None
                key = (name.lower(), ctype)
                if key in company_id:
                    return company_id[key]
                cur.execute(
                    """insert into companies (name, type, default_currency, address)
                       values (%s, %s, %s, %s) returning company_id""",
                    (name, ctype, extra.get("currency"), extra.get("address")))
                cid = cur.fetchone()[0]
                company_id[key] = cid
                created["companies"] += 1
                return cid

            for r in rows:
                currency = (r["currency"] or "").strip().upper()
                if currency not in ("CAD", "USD"):
                    currency = "CAD"  # all rows have it, but stay safe vs CHECK
                s_rate = clean_number(r["shipper_amt"]) or 0
                c_rate = clean_number(r["carrier_amount"]) or 0
                notes = " | ".join(x for x in [(r["remarks"] or "").strip(),
                                               (r["remarks2"] or "").strip()] if x) or None

                shipper_id = get_company(r["customer_name"], "shipper", currency=currency)
                # carrier: stash pay-to address when NOT factored
                carr_addr = (r["factoring2"] or "").strip() if not _flag(r["factoring"]) else None
                carrier_id = get_company(r["carrier_name"], "carrier", currency=currency,
                                         address=carr_addr)
                factoring_id = None
                if _flag(r["factoring"]) and (r["factoring2"] or "").strip():
                    factoring_id = get_company(norm_factoring_name(r["factoring2"]),
                                               "factoring",
                                               address=(r["factoring2"] or "").strip())

                in_recd = parse_date(r["in_received"])
                sent = parse_date(r["invoice_date"])
                pmt = parse_date(r["pmt_revd"])
                has_shipper_doc = bool((r["invoice_date"] or "").strip() or (r["invoice_no"] or "").strip())
                paid = bool(pmt) or (r["status"] or "").lower() == "complete"
                sale = sent or in_recd or date.today()

                load_status = "cancelled" if "cancel" in (r["status"] or "").lower() else (
                    "paid" if paid else ("invoiced" if has_shipper_doc else "confirmed"))

                cur.execute(
                    """insert into loads
                         (load_number, status, currency, shipper_id, carrier_id,
                          shipper_rate, carrier_rate, load_date, sale_date, notes, created_by)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning load_id""",
                    (r["load_no"].strip(), load_status, currency, shipper_id, carrier_id,
                     s_rate, c_rate, sent or in_recd, sale, notes, AUTOMATION_USER_ID))
                load_id = cur.fetchone()[0]
                created["loads"] += 1

                # carrier invoice (AP) — only with a real received date
                if in_recd:
                    rev, disc, note = derive_carrier_review(r["status"], notes or "")
                    cur.execute(
                        """insert into carrier_invoices
                             (load_id, their_invoice_number, received_date, amount,
                              factoring, factoring_company_id, payment_sent,
                              review_status, has_discrepancy, issue_note, created_by)
                           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (load_id, (r["carrier_in_no"] or "").strip() or None, in_recd, c_rate,
                         _flag(r["factoring"]), factoring_id,
                         (r["status"] or "").lower() == "complete",
                         rev, disc, note, AUTOMATION_USER_ID))
                    created["carrier_invoices"] += 1

                # shipper invoice (AR) — only with a real invoice date
                shipper_invoice_id = None
                if sent:
                    hst = clean_number(r["hst"])
                    has_issue = "shipper payment issue" in (r["status"] or "").lower()
                    cur.execute(
                        """insert into shipper_invoices
                             (load_id, our_invoice_number, sent_date, amount, tax_type,
                              tax_amount, status, has_issue, issue_note, created_by)
                           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning shipper_invoice_id""",
                        (load_id, (r["invoice_no"] or "").strip() or r["load_no"].strip(),
                         sent, s_rate, ("HST" if hst else None), hst or 0,
                         ("paid" if paid else "sent"), has_issue,
                         (notes if has_issue else None), AUTOMATION_USER_ID))
                    shipper_invoice_id = cur.fetchone()[0]
                    created["shipper_invoices"] += 1

                # payment received (+ allocation) — only when shipper actually paid
                if pmt and shipper_invoice_id:
                    total = s_rate + (clean_number(r["hst"]) or 0)
                    cur.execute(
                        """insert into payments_received
                             (received_date, total_amount, currency, from_company_id,
                              method, reference, created_by)
                           values (%s,%s,%s,%s,%s,%s,%s) returning payment_id""",
                        (pmt, total, currency, shipper_id,
                         method_for(r["client_cheque_no"], currency),
                         (r["client_cheque_no"] or "").strip() or None, AUTOMATION_USER_ID))
                    payment_id = cur.fetchone()[0]
                    cur.execute(
                        """insert into payment_allocations
                             (payment_id, shipper_invoice_id, amount_allocated)
                           values (%s,%s,%s)""",
                        (payment_id, shipper_invoice_id, total))
                    created["payments"] += 1

        conn.commit()
        logger.info("Promotion committed: {}", dict(created))
    except Exception:
        conn.rollback()
        logger.error("Promotion failed — rolled back, no changes made")
        raise
    finally:
        conn.close()
    return dict(created)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Promote staging → relational model")
    ap.add_argument("--commit", action="store_true", help="WIPE business tables and load for real")
    ap.add_argument("--dry-run", action="store_true", help="print plan only (default)")
    args = ap.parse_args()

    if not USE_DB:
        raise SystemExit("DATABASE_URL not set")

    import psycopg
    conn = psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=15)
    rows = load_staging(conn)
    conn.close()
    print_plan(build_plan(rows))

    if args.commit:
        print("Committing — wiping business tables and rebuilding…")
        print(promote(rows))
    else:
        print("Dry run only. Re-run with --commit to apply.")
