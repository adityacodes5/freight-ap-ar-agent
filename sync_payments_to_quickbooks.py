#!/usr/bin/env python3
"""
Apply payments from the sheet onto the matching QuickBooks invoices / bills.

This is what makes QB show real paid vs outstanding balances (Step 1 of monthly
reconciliation — the invoice-level "who paid what" that the bank statement alone
can't tell you). Bank reconciliation (Step 2, matching against the actual bank
feed) stays a QB/human step in the real company.

  CUSTOMER payments — "Payment Received" tab → QB Payment applied to the Invoice
     (matched by DocNumber = Invoice no). Deposits to Undeposited Funds.
  CARRIER payments  — "ACH/Check" tab (rows with a date mailed) → QB Bill Payment
     applied to the Bill. The ACH tab keys by Load#, so we map Load# → Carrier In#
     (the bill's DocNumber) via Accounts.

Idempotent: skips an invoice/bill whose remaining Balance is already 0, and never
applies more than the outstanding balance.

Usage:
    python sync_payments_to_quickbooks.py --check
    python sync_payments_to_quickbooks.py                 # dry run (both lanes)
    python sync_payments_to_quickbooks.py --ar --limit 5  # customer payments only
    python sync_payments_to_quickbooks.py --ap --limit 5  # carrier payments only
    python sync_payments_to_quickbooks.py --commit
"""
import argparse
import re
import sys

from loguru import logger

from analytics import _client, _cur, _date, _num, ACCOUNTS_TAB
from tools.quickbooks import QuickBooksClient, QuickBooksError

PR_TAB = "Payment Received"
ACH_TAB = "ACH/Check"


def _digits(s) -> str:
    m = re.search(r"\d+", str(s or ""))
    return m.group() if m else ""


def _customer_payments() -> list[dict]:
    """Payment Received: Invoice(0), Amount(1), TOTAL(3), Currency(4), Date(6)."""
    gc = _client()
    out = []
    for r in gc.get_all_values(PR_TAB)[1:]:
        inv = (r[0] if r else "").strip().lstrip("'")
        if not inv:
            continue
        amt = _num(r[3]) if len(r) > 3 and _num(r[3]) else (_num(r[1]) if len(r) > 1 else 0)
        if amt <= 0:
            continue
        d = _date(r[6]) if len(r) > 6 else None
        out.append({"invoice": inv, "amount": round(amt, 2),
                    "date": d.isoformat() if d else None,
                    "currency": _cur(r[4]) if len(r) > 4 else ""})
    return out


def _carrier_payments() -> list[dict]:
    """ACH/Check (mailed rows): Load(0), Amount(6), Currency(7), Date mailed(9),
    mapped to the bill's DocNumber via Accounts (Load no → Carrier In#)."""
    gc = _client()
    acc = gc.get_all_values(ACCOUNTS_TAB)
    h = acc[0]
    li, ci = h.index("Load no"), h.index("Carrier In#")
    load_to_doc = {}
    for r in acc[1:]:
        load = _digits(r[li] if li < len(r) else "")
        doc = (r[ci] if ci < len(r) else "").strip().lstrip("'")
        if load and doc:
            load_to_doc[load] = doc

    out = []
    for r in gc.get_all_values(ACH_TAB)[1:]:
        d = _date(r[9]) if len(r) > 9 else None
        if not d:
            continue  # not actually mailed/paid yet
        amt = _num(r[6]) if len(r) > 6 else 0
        if amt <= 0:
            continue
        load = _digits(r[0] if r else "")
        doc = load_to_doc.get(load)
        if not doc:
            continue
        out.append({"load": load, "doc": doc, "amount": round(amt, 2),
                    "date": d.isoformat(), "currency": _cur(r[7]) if len(r) > 7 else ""})
    return out


def _bal(txn: dict) -> float:
    try:
        return float(txn.get("Balance", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def check() -> None:
    qb = QuickBooksClient()
    info = qb.company_info()
    print(f"Connected to QuickBooks ({qb.environment}): {info.get('CompanyName', '?')}")


def run_ar(qb, rows, commit) -> dict:
    created, skipped, unmatched, errors = 0, 0, 0, 0
    for r in rows:
        head = f"  inv {r['invoice']:>10}  {r['currency']} {r['amount']:>10,.2f}  {r['date'] or '?'}"
        if not commit:
            print(head + "  [dry run]")
            continue
        try:
            inv = qb.find_invoice(r["invoice"])
            if not inv:
                print(head + "  → no matching invoice")
                unmatched += 1
                continue
            bal = _bal(inv)
            if bal <= 0:
                print(head + "  → already paid, skipped")
                skipped += 1
                continue
            applied = min(r["amount"], bal)
            cur = inv.get("CurrencyRef", {}).get("value")
            er = qb.exchange_rate(cur, r["date"])
            qb.create_payment(customer_ref=inv["CustomerRef"]["value"], invoice_id=inv["Id"],
                              amount=applied, txn_date=r["date"], currency=cur, exchange_rate=er)
            print(head + f"  → paid {applied:,.2f}")
            created += 1
        except QuickBooksError as e:
            print(head + f"  → ERROR: {e}")
            errors += 1
    return {"created": created, "skipped": skipped, "unmatched": unmatched, "errors": errors}


def run_ap(qb, rows, commit) -> dict:
    created, skipped, unmatched, errors = 0, 0, 0, 0
    bank_cache: dict[str, str] = {}
    for r in rows:
        head = f"  load {r['load']:>6} bill {r['doc'][:12]:<12}  {r['currency']} {r['amount']:>10,.2f}  {r['date']}"
        if not commit:
            print(head + "  [dry run]")
            continue
        try:
            bill = qb.find_bill(r["doc"])
            if not bill:
                print(head + "  → no matching bill")
                unmatched += 1
                continue
            bal = _bal(bill)
            if bal <= 0:
                print(head + "  → already paid, skipped")
                skipped += 1
                continue
            applied = min(r["amount"], bal)
            cur = bill.get("CurrencyRef", {}).get("value")
            if cur not in bank_cache:
                bank_cache[cur] = qb.find_bank_account(cur)["Id"]
            er = qb.exchange_rate(cur, r["date"])
            qb.create_bill_payment(vendor_ref=bill["VendorRef"]["value"], bill_id=bill["Id"],
                                   amount=applied, bank_account_ref=bank_cache[cur],
                                   txn_date=r["date"], currency=cur, exchange_rate=er)
            print(head + f"  → paid {applied:,.2f}")
            created += 1
        except QuickBooksError as e:
            print(head + f"  → ERROR: {e}")
            errors += 1
    return {"created": created, "skipped": skipped, "unmatched": unmatched, "errors": errors}


def run(commit=False, limit=None, ar=True, ap=True) -> None:
    qb = QuickBooksClient() if commit else None
    if ar:
        rows = _customer_payments()
        if limit:
            rows = rows[:limit]
        print(f"\nCUSTOMER payments — {len(rows)} row(s). "
              f"{'COMMIT' if commit else 'DRY RUN'}\n")
        s = run_ar(qb, rows, commit)
        if commit:
            print(f"  AR: {s['created']} applied, {s['skipped']} already paid, "
                  f"{s['unmatched']} unmatched, {s['errors']} errors")
    if ap:
        rows = _carrier_payments()
        if limit:
            rows = rows[:limit]
        print(f"\nCARRIER payments — {len(rows)} row(s). "
              f"{'COMMIT' if commit else 'DRY RUN'}\n")
        s = run_ap(qb, rows, commit)
        if commit:
            print(f"  AP: {s['created']} applied, {s['skipped']} already paid, "
                  f"{s['unmatched']} unmatched, {s['errors']} errors")


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply sheet payments onto QuickBooks invoices/bills.")
    ap.add_argument("--check", action="store_true", help="test the QB connection and exit")
    ap.add_argument("--commit", action="store_true", help="actually create payments (default: dry run)")
    ap.add_argument("--limit", type=int, default=None, help="only process the first N rows per lane")
    ap.add_argument("--ar", action="store_true", help="customer payments only")
    ap.add_argument("--ap", action="store_true", help="carrier payments only")
    args = ap.parse_args()

    # default: both lanes
    do_ar = args.ar or not args.ap
    do_ap = args.ap or not args.ar
    try:
        if args.check:
            check()
            return
        run(commit=args.commit, limit=args.limit, ar=do_ar, ap=do_ap)
    except QuickBooksError as e:
        logger.error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
