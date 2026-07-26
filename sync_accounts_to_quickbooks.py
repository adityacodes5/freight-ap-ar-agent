#!/usr/bin/env python3
"""
Sync shipper (AR) invoices from the Accounts sheet into QuickBooks Online.

Workflow this supports: invoices go onto QuickBooks as they come; payments get
matched to them in QB (manually for now, automated later). This pushes ONE
Invoice per invoiced load, keyed by DocNumber = the sheet's Invoice no, so it is
idempotent — re-running skips invoices that already exist in QB.

  • Customer   = "Customer Name"     (find-or-create)
  • DocNumber  = "Invoice no"        (falls back to Load no)
  • TxnDate    = "invoice date"
  • DueDate    = "Due date"
  • Line 1     = "Shipper Amt"        ("Freight — load <n>")
  • Line 2     = "HST"                (only when > 0)
  • Currency   = "Currency"           (CAD / USD — needs multicurrency on)
  • Skipped    : STATUS "load cancelled", or no Shipper Amt, or no invoice date

Usage:
    python sync_accounts_to_quickbooks.py --check          # test connection only
    python sync_accounts_to_quickbooks.py                  # dry run (default)
    python sync_accounts_to_quickbooks.py --limit 5        # dry run, first 5
    python sync_accounts_to_quickbooks.py --commit         # actually create in QB
    python sync_accounts_to_quickbooks.py --commit --limit 3
"""
import argparse
import sys

from loguru import logger

from analytics import _client, _cur, _date, _num, ACCOUNTS_TAB
from tools.quickbooks import QuickBooksClient, QuickBooksError

ITEM_NAME = "Freight Brokerage"


def _rows() -> list[dict]:
    """Read Accounts and return the invoiceable rows as normalized dicts."""
    gc = _client()
    acc = gc.get_all_values(ACCOUNTS_TAB)
    h = acc[0]
    cols = {n: h.index(n) for n in ("Load no", "Invoice no", "Customer Name",
                                    "invoice date", "Shipper Amt", "HST", "Due date",
                                    "Currency", "STATUS")}

    def g(r, name):
        i = cols[name]
        return (r[i] if i < len(r) else "").strip()

    out = []
    for r in acc[1:]:
        status = g(r, "STATUS").lower()
        if status == "load cancelled":
            continue
        shipper = _num(g(r, "Shipper Amt"))
        if shipper <= 0:
            continue
        inv_date = _date(g(r, "invoice date"))
        if not inv_date:
            continue  # not actually invoiced yet
        customer = g(r, "Customer Name")
        if not customer:
            continue
        load = g(r, "Load no").lstrip("'")
        doc = g(r, "Invoice no").lstrip("'") or load
        due = _date(g(r, "Due date"))
        out.append({
            "load": load,
            "doc_number": doc,
            "customer": customer,
            "txn_date": inv_date.isoformat(),
            "due_date": due.isoformat() if due else None,
            "shipper": round(shipper, 2),
            "hst": round(_num(g(r, "HST")), 2),
            "currency": _cur(g(r, "Currency")),
        })
    return out


def _lines(row: dict) -> list[dict]:
    lines = [{"amount": row["shipper"], "description": f"Freight — load {row['load']}"}]
    if row["hst"] > 0:
        lines.append({"amount": row["hst"], "description": "HST"})
    return lines


def check() -> None:
    qb = QuickBooksClient()
    info = qb.company_info()
    name = info.get("CompanyName", "?")
    country = info.get("Country", "")
    print(f"Connected to QuickBooks ({qb.environment}): {name} [{country}]")
    print(f"  Realm: {qb.realm_id}")


def run(commit: bool = False, limit: int | None = None) -> dict:
    rows = _rows()
    if limit:
        rows = rows[:limit]

    total = len(rows)
    by_cur: dict[str, int] = {}
    for r in rows:
        by_cur[r["currency"]] = by_cur.get(r["currency"], 0) + 1
    print(f"\n{total} invoiceable load(s) found "
          f"({', '.join(f'{v} {k}' for k, v in sorted(by_cur.items()))}).")
    print(f"Mode: {'COMMIT — creating in QuickBooks' if commit else 'DRY RUN — nothing sent'}\n")

    qb = None
    item_ref = None
    if commit:
        qb = QuickBooksClient()
        item_ref = qb.find_or_create_item(ITEM_NAME)["Id"]

    created, skipped, errors = 0, 0, 0
    for r in rows:
        tot = r["shipper"] + r["hst"]
        label = (f"  {r['doc_number']:>10}  {r['customer'][:28]:<28} "
                 f"{r['currency']} {tot:>10,.2f}  {r['txn_date']}"
                 f"{'  due ' + r['due_date'] if r['due_date'] else ''}")
        if not commit:
            print(label + "  [dry run]")
            continue
        try:
            if qb.find_invoice(r["doc_number"]):
                print(label + "  → already in QB, skipped")
                skipped += 1
                continue
            cust = qb.find_or_create_customer(r["customer"], r["currency"])
            er = qb.exchange_rate(r["currency"], r["txn_date"])
            inv = qb.create_invoice(
                customer_ref=cust["Id"], lines=_lines(r), item_ref=item_ref,
                doc_number=r["doc_number"], txn_date=r["txn_date"],
                due_date=r["due_date"], currency=r["currency"], exchange_rate=er,
                memo=f"Load {r['load']}",
            )
            print(label + f"  → created (QB Id {inv['Id']})")
            created += 1
        except QuickBooksError as e:
            print(label + f"  → ERROR: {e}")
            errors += 1

    summary = {"total": total, "created": created, "skipped": skipped, "errors": errors}
    if commit:
        print(f"\nDone: {created} created, {skipped} already present, {errors} errors.")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync Accounts AR invoices to QuickBooks Online.")
    ap.add_argument("--check", action="store_true", help="test the QB connection and exit")
    ap.add_argument("--commit", action="store_true", help="actually create invoices (default: dry run)")
    ap.add_argument("--limit", type=int, default=None, help="only process the first N rows")
    args = ap.parse_args()

    try:
        if args.check:
            check()
            return
        run(commit=args.commit, limit=args.limit)
    except QuickBooksError as e:
        logger.error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
