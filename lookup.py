"""Global load lookup — the full money lifecycle of one load, read-only, pulled
from Accounts + Payment Received + ACH/Check so you can answer "what happened to
load 5xxx?" (shipper invoice -> carrier invoice -> carrier paid -> customer paid ->
margin) in one place.
"""
import re

from analytics import _client, _cur, _date, _num, ACCOUNTS_TAB

PR_TAB = "Payment Received"
ACH_TAB = "ACH/Check"


def _digits(s) -> str:
    m = re.search(r"\d+", str(s or ""))
    return m.group() if m else ""


def load_lookup(load_no: str) -> dict:
    load = _digits(load_no)
    if not load:
        return {"found": False, "load": load_no, "error": "No load number."}
    gc = _client()

    acc = gc.get_all_values(ACCOUNTS_TAB)
    h = acc[0]

    def ix(name):
        return h.index(name)

    cols = {n: ix(n) for n in ("Load no", "Invoice no", "Customer Name", "invoice date",
                               "Shipper Amt", "HST", "Due date", "PMT Revd", "Carrier name",
                               "Carrier In#", "Payment due date ", "Carrier Amount",
                               "Currency", "STATUS", "Remarks")}

    row = None
    for r in acc[1:]:
        if _digits(r[cols["Load no"]] if cols["Load no"] < len(r) else "") == load:
            row = r
            break
    if row is None:
        return {"found": False, "load": load}

    def g(name):
        i = cols[name]
        return (row[i] if i < len(row) else "").strip()

    invoice = g("Invoice no") or load
    shipper_amt = _num(g("Shipper Amt"))
    hst = _num(g("HST"))
    carrier_amt = _num(g("Carrier Amount"))
    pmt_revd = _date(g("PMT Revd"))
    currency = _cur(g("Currency"))
    status = g("STATUS")
    account = {
        "invoice": invoice, "customer": g("Customer Name"),
        "invoice_date": g("invoice date").lstrip("'"), "shipper_amt": shipper_amt, "hst": hst,
        "due_date": g("Due date").lstrip("'"), "pmt_revd": g("PMT Revd").lstrip("'"),
        "carrier_name": g("Carrier name"), "carrier_in": g("Carrier In#"),
        "carrier_due": g("Payment due date ").lstrip("'"), "carrier_amount": carrier_amt,
        "currency": currency, "status": status, "remarks": g("Remarks"),
        "margin": round(shipper_amt - carrier_amt, 2) if (shipper_amt and carrier_amt) else None,
    }

    # Customer payments (Payment Received rows whose Invoice matches)
    pr_rows = []
    for r in gc.get_all_values(PR_TAB)[1:]:
        if _digits(r[0] if r else "") == _digits(invoice):
            pr_rows.append({"amount": _num(r[3]) if len(r) > 3 and _num(r[3]) else _num(r[1] if len(r) > 1 else 0),
                            "currency": _cur(r[4]) if len(r) > 4 else "", "check": (r[5] if len(r) > 5 else "").strip(),
                            "date": (r[6] if len(r) > 6 else "").strip().lstrip("'")})

    # Carrier payments (ACH/Check rows whose Load matches)
    ach_rows = []
    for r in gc.get_all_values(ACH_TAB)[1:]:
        if _digits(r[0] if r else "") == load:
            mailed = _date(r[9]) if len(r) > 9 else None
            ach_rows.append({"payee": (r[1] if len(r) > 1 else "").strip(),
                             "amount": _num(r[6]) if len(r) > 6 else 0, "currency": _cur(r[7]) if len(r) > 7 else "",
                             "method": (r[8] if len(r) > 8 else "").strip(),
                             "date_mailed": (r[9] if len(r) > 9 else "").strip().lstrip("'"),
                             "status": (r[11] if len(r) > 11 else "").strip(), "mailed": bool(mailed)})

    carrier_paid = any(a["mailed"] for a in ach_rows) or status.lower() == "complete"
    customer_paid = pmt_revd is not None

    timeline = [
        {"stage": "booked", "label": "Load booked in Accounts", "done": True},
        {"stage": "shipper_invoice", "label": "Shipper invoice entered", "done": shipper_amt > 0,
         "detail": f"{currency} {shipper_amt:,.2f}" if shipper_amt else ""},
        {"stage": "carrier_invoice", "label": "Carrier invoice received", "done": bool(account["carrier_in"]),
         "detail": account["carrier_in"]},
        {"stage": "carrier_paid", "label": "Carrier paid (AP)", "done": carrier_paid,
         "detail": next((a["date_mailed"] for a in ach_rows if a["mailed"]), "") or (status if status.lower() == "complete" else "")},
        {"stage": "customer_paid", "label": "Customer paid (AR)", "done": customer_paid,
         "detail": account["pmt_revd"] if customer_paid else ""},
    ]

    return {"found": True, "load": load, "account": account,
            "payments_received": pr_rows, "ach_entries": ach_rows,
            "carrier_paid": carrier_paid, "customer_paid": customer_paid,
            "timeline": timeline}
