#!/usr/bin/env python3
"""
Sync carrier (AP) bills from the Accounts sheet into QuickBooks Online.

The mirror of sync_accounts_to_quickbooks.py: that one pushes what customers owe
US (AR invoices); this one pushes what WE owe carriers (AP bills), so QuickBooks
tracks both sides and you can reconcile carrier payments there too.

One Bill per load that has a received carrier invoice, keyed by DocNumber =
"Carrier In#" (the carrier's own invoice number), so it is idempotent.

  • Vendor    = "Carrier name"        (find-or-create)
  • DocNumber = "Carrier In#"         (the carrier's invoice number)
  • TxnDate   = "invoice date"        (load invoice date, used as the bill date proxy —
                                       the sheet has no clean carrier-invoice date column)
  • DueDate   = "Payment due date "
  • Line      = "Carrier Amount"      ("Freight — load <n>") on a COGS/Expense account
  • Currency  = "Currency"            (CAD / USD)
  • Skipped   : STATUS "load cancelled", no Carrier Amount, or no Carrier In#

Usage (same flags as the invoice sync):
    python sync_carrier_bills_to_quickbooks.py --check
    python sync_carrier_bills_to_quickbooks.py                 # dry run
    python sync_carrier_bills_to_quickbooks.py --limit 5
    python sync_carrier_bills_to_quickbooks.py --commit --limit 3
    python sync_carrier_bills_to_quickbooks.py --commit
"""
import argparse
import sys

from loguru import logger

from analytics import _client, _cur, _date, _num, ACCOUNTS_TAB
from tools.quickbooks import QuickBooksClient, QuickBooksError


def _rows() -> list[dict]:
    gc = _client()
    acc = gc.get_all_values(ACCOUNTS_TAB)
    h = acc[0]
    cols = {n: h.index(n) for n in ("Load no", "Carrier name", "Carrier In#",
                                    "invoice date", "Payment due date ",
                                    "Carrier Amount", "Currency", "STATUS")}

    def g(r, name):
        i = cols[name]
        return (r[i] if i < len(r) else "").strip()

    out = []
    for r in acc[1:]:
        if g(r, "STATUS").lower() == "load cancelled":
            continue
        amount = _num(g(r, "Carrier Amount"))
        if amount <= 0:
            continue
        doc = g(r, "Carrier In#").lstrip("'")
        if not doc:
            continue  # no carrier invoice received yet — nothing to bill
        vendor = g(r, "Carrier name")
        if not vendor:
            continue
        load = g(r, "Load no").lstrip("'")
        txn = _date(g(r, "invoice date"))
        due = _date(g(r, "Payment due date "))
        out.append({
            "load": load,
            "doc_number": doc,
            "vendor": vendor,
            "txn_date": (txn or due).isoformat() if (txn or due) else None,
            "due_date": due.isoformat() if due else None,
            "amount": round(amount, 2),
            "currency": _cur(g(r, "Currency")),
        })
    return out


def check() -> None:
    qb = QuickBooksClient()
    info = qb.company_info()
    print(f"Connected to QuickBooks ({qb.environment}): {info.get('CompanyName', '?')}")
    print(f"  Realm: {qb.realm_id}")


def run(commit: bool = False, limit: int | None = None) -> dict:
    rows = _rows()
    if limit:
        rows = rows[:limit]

    by_cur: dict[str, int] = {}
    for r in rows:
        by_cur[r["currency"]] = by_cur.get(r["currency"], 0) + 1
    print(f"\n{len(rows)} billable load(s) found "
          f"({', '.join(f'{v} {k}' for k, v in sorted(by_cur.items()))}).")
    print(f"Mode: {'COMMIT — creating in QuickBooks' if commit else 'DRY RUN — nothing sent'}\n")

    qb = None
    account_ref = None
    if commit:
        qb = QuickBooksClient()
        account_ref = qb.find_expense_account()["Id"]

    created, skipped, errors = 0, 0, 0
    for r in rows:
        label = (f"  {r['doc_number'][:14]:>14}  {r['vendor'][:26]:<26} "
                 f"{r['currency']} {r['amount']:>10,.2f}  "
                 f"{r['txn_date'] or '?':<10}"
                 f"{'  due ' + r['due_date'] if r['due_date'] else ''}")
        if not commit:
            print(label + "  [dry run]")
            continue
        try:
            if qb.find_bill(r["doc_number"]):
                print(label + "  → already in QB, skipped")
                skipped += 1
                continue
            vend = qb.find_or_create_vendor(r["vendor"], r["currency"])
            er = qb.exchange_rate(r["currency"], r["txn_date"])
            bill = qb.create_bill(
                vendor_ref=vend["Id"], account_ref=account_ref,
                lines=[{"amount": r["amount"], "description": f"Freight — load {r['load']}"}],
                doc_number=r["doc_number"], txn_date=r["txn_date"],
                due_date=r["due_date"], currency=r["currency"], exchange_rate=er,
                memo=f"Load {r['load']}",
            )
            print(label + f"  → created (QB Id {bill['Id']})")
            created += 1
        except QuickBooksError as e:
            print(label + f"  → ERROR: {e}")
            errors += 1

    if commit:
        print(f"\nDone: {created} created, {skipped} already present, {errors} errors.")
    return {"total": len(rows), "created": created, "skipped": skipped, "errors": errors}


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync Accounts carrier bills to QuickBooks Online.")
    ap.add_argument("--check", action="store_true", help="test the QB connection and exit")
    ap.add_argument("--commit", action="store_true", help="actually create bills (default: dry run)")
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
