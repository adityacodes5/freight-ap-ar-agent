"""Read-only analytics over the Accounts workbook for the dashboard.

Two views, both computed live from the sheets (no writes):

  cashflow()  — monthly INFLOW (customer payments, the "Payment Received" tab) and
                OUTFLOW (carrier payments, the "ACH/Check" tab), split by currency.
                CAD and USD are never mixed (no FX assumption).

  overdue()   — receivables owed to us and past due: an Accounts row with a Shipper
                Amt, no payment received yet (PMT Revd blank), and a due date in the
                past. Includes per-currency totals and aging buckets.
"""
import datetime as dt
import re

from config import Config
from db import cache_get, cache_set
from tools.sheets import GoogleSheetsClient

PAYMENTS_TAB = "Payment Received"
ACH_TAB = "ACH/Check"
ACCOUNTS_TAB = "Accounts"


def _num(s) -> float:
    """Parse a money cell like '3,704.40' or '$1,200' → float (0.0 if not a number)."""
    s = str(s or "").replace(",", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _date(s):
    """Parse a date cell, tolerating a leading apostrophe and several formats."""
    s = str(s or "").strip().lstrip("'").strip()
    if not s or s.upper().startswith("#N/A"):
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _cur(s) -> str:
    c = str(s or "").strip().upper()
    return c if c in ("CAD", "USD") else "OTHER"


def _client():
    return GoogleSheetsClient(Config())


# ── Cache wrapper ─────────────────────────────────────────────────────────────
# Every view here rescans whole sheets, so we memoise the computed payload in
# Postgres (see db.cache_*). The dashboard's ↻ button passes refresh=True to force
# a recompute; otherwise a result younger than `ttl` is served straight from PG.
def cached(key: str, ttl: int, compute, refresh: bool = False) -> dict:
    if not refresh:
        hit = cache_get(key, ttl)
        if hit is not None:
            payload, age = hit
            payload["_cache"] = {"hit": True, "age_s": age}
            return payload
    payload = compute()
    cache_set(key, payload)
    payload["_cache"] = {"hit": False, "age_s": 0}
    return payload


# ── Monthly cash flow ─────────────────────────────────────────────────────────
def cashflow(months_back: int = 18) -> dict:
    """Monthly inflow (payments received) and outflow (carrier payments), by
    currency. Returns the most recent `months_back` months."""
    gc = _client()

    # month key -> {inflow_cad, inflow_usd, outflow_cad, outflow_usd}
    buckets: dict[str, dict] = {}

    def bump(d, cur, amt, side):
        if not d or amt <= 0 or cur == "OTHER":
            return
        key = f"{d.year:04d}-{d.month:02d}"
        b = buckets.setdefault(key, {"inflow_cad": 0.0, "inflow_usd": 0.0,
                                     "outflow_cad": 0.0, "outflow_usd": 0.0})
        b[f"{side}_{cur.lower()}"] += amt

    # Inflow — Payment Received: TOTAL(4) or Amount(2), Currency(5), Date(7)
    pr = gc.get_all_values(PAYMENTS_TAB)
    for row in pr[1:]:
        d = _date(row[6]) if len(row) > 6 else None
        amt = _num(row[3]) if len(row) > 3 and _num(row[3]) else (_num(row[1]) if len(row) > 1 else 0)
        cur = _cur(row[4]) if len(row) > 4 else "OTHER"
        bump(d, cur, amt, "inflow")

    # Outflow — ACH/Check: Amount(6), currency(7), date mailed(9)
    ach = gc.get_all_values(ACH_TAB)
    for row in ach[1:]:
        d = _date(row[9]) if len(row) > 9 else None
        amt = _num(row[6]) if len(row) > 6 else 0
        cur = _cur(row[7]) if len(row) > 7 else "OTHER"
        bump(d, cur, amt, "outflow")

    keys = sorted(buckets)[-months_back:]
    months = []
    for k in keys:
        b = buckets[k]
        y, m = k.split("-")
        label = dt.date(int(y), int(m), 1).strftime("%b %Y")
        months.append({
            "month": k, "label": label,
            "inflow_cad": round(b["inflow_cad"], 2), "inflow_usd": round(b["inflow_usd"], 2),
            "outflow_cad": round(b["outflow_cad"], 2), "outflow_usd": round(b["outflow_usd"], 2),
            "net_cad": round(b["inflow_cad"] - b["outflow_cad"], 2),
            "net_usd": round(b["inflow_usd"] - b["outflow_usd"], 2),
        })

    def _tot(field):
        return round(sum(b[field] for b in buckets.values()), 2)

    totals = {
        "inflow_cad": _tot("inflow_cad"), "inflow_usd": _tot("inflow_usd"),
        "outflow_cad": _tot("outflow_cad"), "outflow_usd": _tot("outflow_usd"),
    }

    # "If everything due today were settled" — actual flows plus what's still
    # outstanding and past due: overdue AR would be collected, overdue AP paid.
    ar = overdue()["totals"]           # {CAD, USD} still owed to us
    ap = overdue_payables()["totals"]  # {CAD, USD} we still owe carriers
    outstanding = {
        "ar_cad": ar.get("CAD", 0), "ar_usd": ar.get("USD", 0),
        "ap_cad": ap.get("CAD", 0), "ap_usd": ap.get("USD", 0),
    }
    expected = {
        "inflow_cad": round(totals["inflow_cad"] + outstanding["ar_cad"], 2),
        "inflow_usd": round(totals["inflow_usd"] + outstanding["ar_usd"], 2),
        "outflow_cad": round(totals["outflow_cad"] + outstanding["ap_cad"], 2),
        "outflow_usd": round(totals["outflow_usd"] + outstanding["ap_usd"], 2),
    }

    return {
        "months": months,
        "totals": totals,
        "outstanding": outstanding,
        "expected": expected,
        "as_of": dt.date.today().isoformat(),
    }


# ── Overdue receivables (AR) ──────────────────────────────────────────────────
_AGING_BUCKETS = [(1, 30), (31, 60), (61, 90), (91, 10_000)]


def overdue(today: dt.date | None = None) -> dict:
    """Invoices owed to us and past due (Shipper Amt present, PMT Revd blank,
    due date in the past). Sorted most-overdue first."""
    gc = _client()
    today = today or dt.date.today()
    acc = gc.get_all_values(gc.config.accounts_sheet_name) if hasattr(gc, "config") else gc.get_all_values("Accounts")
    h = acc[0]

    def idx(name):
        return h.index(name)

    i_load, i_cust, i_inv, i_sa, i_hst, i_due, i_pmt, i_cur, i_status = (
        idx("Load no"), idx("Customer Name"), idx("invoice date"), idx("Shipper Amt"),
        idx("HST"), idx("Due date"), idx("PMT Revd"), idx("Currency"), idx("STATUS"))

    rows = []
    for row in acc[1:]:
        load = (row[i_load] if i_load < len(row) else "").strip()
        if not re.search(r"\d", load):
            continue
        amt = _num(row[i_sa]) if i_sa < len(row) else 0
        if amt <= 0:
            continue
        # PMT Revd holds the date the customer's payment was received. A real date
        # → paid (skip). Blank or "#N/A" (a VLOOKUP miss) → still owed.
        if _date(row[i_pmt] if i_pmt < len(row) else ""):
            continue
        status = (row[i_status] if i_status < len(row) else "").strip().lower()
        if status == "load cancelled":
            continue
        due = _date(row[i_due]) if i_due < len(row) else None
        if not due or due >= today:
            continue  # not overdue (or no due date)
        hst = _num(row[i_hst]) if i_hst < len(row) else 0
        rows.append({
            "load": load,
            "customer": (row[i_cust] if i_cust < len(row) else "").strip(),
            "invoice_date": (row[i_inv] if i_inv < len(row) else "").strip().lstrip("'"),
            "due_date": due.isoformat(),
            "days_overdue": (today - due).days,
            "amount": round(amt + hst, 2),
            "currency": _cur(row[i_cur]) if i_cur < len(row) else "OTHER",
        })

    rows.sort(key=lambda r: r["days_overdue"], reverse=True)

    totals: dict[str, float] = {}
    buckets = {f"{lo}-{hi if hi < 9999 else '90+'}": {} for lo, hi in _AGING_BUCKETS}
    by_customer: dict[tuple, float] = {}
    for r in rows:
        cur = r["currency"]
        totals[cur] = round(totals.get(cur, 0) + r["amount"], 2)
        for lo, hi in _AGING_BUCKETS:
            if lo <= r["days_overdue"] <= hi:
                label = f"{lo}-{hi if hi < 9999 else '90+'}"
                buckets[label][cur] = round(buckets[label].get(cur, 0) + r["amount"], 2)
                break
        key = (r["customer"], cur)
        by_customer[key] = round(by_customer.get(key, 0) + r["amount"], 2)

    top = sorted(({"customer": c, "currency": cur, "amount": a}
                  for (c, cur), a in by_customer.items()),
                 key=lambda x: x["amount"], reverse=True)[:15]

    return {
        "rows": rows,
        "count": len(rows),
        "totals": totals,
        "aging_buckets": buckets,
        "top_customers": top,
        "as_of": today.isoformat(),
    }


# ── Overdue payables (AP — money we owe carriers) ─────────────────────────────
def overdue_payables(today: dt.date | None = None) -> dict:
    """Carrier invoices past due that we haven't paid yet — read PURELY from the
    Accounts sheet. A row is an open payable when it has a carrier invoice
    (Carrier In#), a carrier payment due date that's in the past, and its STATUS
    is NOT Complete / Load Cancelled. STATUS is the source of truth for "paid";
    we deliberately do NOT cross-reference the ACH/Check sheet, because a load can
    be queued/mailed there before it's reconciled and marked Complete in Accounts
    (that dropped ~90% of the real open payables)."""
    gc = _client()
    today = today or dt.date.today()
    acc = gc.get_all_values(ACCOUNTS_TAB)
    h = acc[0]

    def i(name):
        return h.index(name)

    i_load, i_cin, i_carrier, i_due, i_amt, i_cur, i_status = (
        i("Load no"), i("Carrier In#"), i("Carrier name"), i("Payment due date "),
        i("Carrier Amount"), i("Currency"), i("STATUS"))
    fact_cols = [j for j, x in enumerate(h) if x.strip() == "Factoring"]
    i_factco = fact_cols[-1] if fact_cols else i_carrier

    rows = []
    for row in acc[1:]:
        m = re.search(r"\d+", row[i_load] if i_load < len(row) else "")
        if not m:
            continue
        load = m.group()
        if not (row[i_cin].strip() if i_cin < len(row) else ""):
            continue  # no carrier invoice received
        due = _date(row[i_due]) if i_due < len(row) else None
        if not due:
            continue  # no carrier payment due date → not a scheduled payable
        status = (row[i_status] if i_status < len(row) else "").strip()
        sl = status.lower()
        if sl in ("complete", "load cancelled"):
            continue  # paid / done / cancelled per Accounts STATUS
        if due >= today:
            continue  # not yet overdue
        amt = _num(row[i_amt]) if i_amt < len(row) else 0
        payee = (row[i_factco] if i_factco < len(row) else "").strip() or (row[i_carrier] if i_carrier < len(row) else "").strip()
        rows.append({
            "load": load,
            "carrier": (row[i_carrier] if i_carrier < len(row) else "").strip(),
            "payee": payee,
            "due_date": due.isoformat(),
            "days_overdue": (today - due).days,
            "amount": round(amt, 2),
            "currency": _cur(row[i_cur]) if i_cur < len(row) else "OTHER",
            "flagged": sl == "carrier payment issue",
        })

    rows.sort(key=lambda r: r["days_overdue"], reverse=True)

    totals: dict[str, float] = {}
    buckets = {f"{lo}-{hi if hi < 9999 else '90+'}": {} for lo, hi in _AGING_BUCKETS}
    by_payee: dict[tuple, float] = {}
    for r in rows:
        cur = r["currency"]
        totals[cur] = round(totals.get(cur, 0) + r["amount"], 2)
        for lo, hi in _AGING_BUCKETS:
            if lo <= r["days_overdue"] <= hi:
                label = f"{lo}-{hi if hi < 9999 else '90+'}"
                buckets[label][cur] = round(buckets[label].get(cur, 0) + r["amount"], 2)
                break
        by_payee[(r["payee"], cur)] = round(by_payee.get((r["payee"], cur), 0) + r["amount"], 2)

    top = sorted(({"payee": p, "currency": cur, "amount": a} for (p, cur), a in by_payee.items()),
                 key=lambda x: x["amount"], reverse=True)[:15]

    return {
        "rows": rows, "count": len(rows), "totals": totals,
        "aging_buckets": buckets, "top_payees": top, "as_of": today.isoformat(),
    }


# ── Carrier scorecard ─────────────────────────────────────────────────────────
def carrier_scorecard(today: dt.date | None = None) -> dict:
    """Per-carrier reliability + value from our side: how many loads, what we've
    spent, the margin we earn on their loads, and how often their loads get
    flagged (STATUS 'carrier payment issue'). One row per carrier."""
    gc = _client()
    today = today or dt.date.today()
    acc = gc.get_all_values(ACCOUNTS_TAB)
    h = acc[0]

    def i(name):
        return h.index(name)

    i_carrier, i_sa, i_ca, i_cur, i_status, i_cin = (
        i("Carrier name"), i("Shipper Amt"), i("Carrier Amount"),
        i("Currency"), i("STATUS"), i("Carrier In#"))

    agg: dict[str, dict] = {}
    for row in acc[1:]:
        name = (row[i_carrier] if i_carrier < len(row) else "").strip()
        if not name:
            continue
        key = name.upper()
        c = agg.setdefault(key, {"carrier": name, "loads": 0, "spend": 0.0, "revenue": 0.0,
                                 "margin": 0.0, "margin_loads": 0, "issues": 0,
                                 "cur": {"CAD": 0, "USD": 0}})
        ca = _num(row[i_ca]) if i_ca < len(row) else 0
        sa = _num(row[i_sa]) if i_sa < len(row) else 0
        status = (row[i_status] if i_status < len(row) else "").strip().lower()
        cur = _cur(row[i_cur]) if i_cur < len(row) else "OTHER"
        c["loads"] += 1
        c["spend"] += ca
        if cur in ("CAD", "USD"):
            c["cur"][cur] += 1
        if status == "carrier payment issue":
            c["issues"] += 1
        if sa > 0 and ca > 0:
            c["revenue"] += sa
            c["margin"] += sa - ca
            c["margin_loads"] += 1

    rows = []
    for c in agg.values():
        dom = "CAD" if c["cur"]["CAD"] >= c["cur"]["USD"] else "USD"
        rows.append({
            "carrier": c["carrier"], "loads": c["loads"],
            "spend": round(c["spend"], 2), "avg_load": round(c["spend"] / c["loads"], 2) if c["loads"] else 0,
            "revenue": round(c["revenue"], 2), "margin": round(c["margin"], 2),
            "margin_pct": round(c["margin"] / c["revenue"] * 100, 1) if c["revenue"] else 0.0,
            "issues": c["issues"],
            "issue_pct": round(c["issues"] / c["loads"] * 100, 0) if c["loads"] else 0,
            "currency": dom,
        })
    rows.sort(key=lambda r: r["spend"], reverse=True)
    return {"carriers": rows, "count": len(rows), "as_of": today.isoformat()}


# ── Payables outlook (for the daily notification) ─────────────────────────────
def payables_snapshot(days: int = 7, today: dt.date | None = None) -> dict:
    """One-pass carrier-payables outlook for the notification email: what's already
    overdue and what falls due within the next `days` (the weekly pay-run window).
    Same paid signal as overdue_payables — STATUS Complete/Cancelled = paid; read
    purely from Accounts (no ACH cross-reference)."""
    gc = _client()
    today = today or dt.date.today()
    acc = gc.get_all_values(ACCOUNTS_TAB)
    h = acc[0]

    def i(name):
        return h.index(name)

    i_load, i_carrier, i_cin, i_due, i_amt, i_cur, i_status = (
        i("Load no"), i("Carrier name"), i("Carrier In#"), i("Payment due date "),
        i("Carrier Amount"), i("Currency"), i("STATUS"))
    fact_cols = [j for j, x in enumerate(h) if x.strip() == "Factoring"]
    i_factco = fact_cols[-1] if fact_cols else i_carrier
    horizon = today + dt.timedelta(days=days)

    overdue = {"count": 0, "CAD": 0.0, "USD": 0.0}
    due_soon = {"count": 0, "CAD": 0.0, "USD": 0.0}
    soon_rows = []
    for row in acc[1:]:
        m = re.search(r"\d+", row[i_load] if i_load < len(row) else "")
        if not m:
            continue
        if not (row[i_cin].strip() if i_cin < len(row) else ""):
            continue  # no carrier invoice received
        due = _date(row[i_due]) if i_due < len(row) else None
        if not due:
            continue
        sl = (row[i_status] if i_status < len(row) else "").strip().lower()
        if sl in ("complete", "load cancelled"):
            continue
        cur = _cur(row[i_cur]) if i_cur < len(row) else "OTHER"
        if cur == "OTHER":
            cur = "USD"  # keep it counted; most unlabeled carrier pays are USD
        amt = _num(row[i_amt]) if i_amt < len(row) else 0
        payee = (row[i_factco] if i_factco < len(row) else "").strip() or \
                (row[i_carrier] if i_carrier < len(row) else "").strip()
        if due < today:
            overdue["count"] += 1
            overdue[cur] = round(overdue[cur] + amt, 2)
        elif due <= horizon:
            due_soon["count"] += 1
            due_soon[cur] = round(due_soon[cur] + amt, 2)
            soon_rows.append({"load": m.group(), "payee": payee,
                              "due_date": due.isoformat(), "days_until": (due - today).days,
                              "amount": round(amt, 2), "currency": cur})
    soon_rows.sort(key=lambda r: r["days_until"])
    return {"overdue": overdue, "due_soon": due_soon, "soon_rows": soon_rows,
            "days": days, "as_of": today.isoformat()}


# ── Business performance (margins + how fast we get paid) ─────────────────────
#   (low, high, label) — days-to-pay buckets. Last bucket is open-ended.
_PAYSPEED_BUCKETS = [(0, 15, "0-15"), (16, 30, "16-30"), (31, 45, "31-45"),
                     (46, 60, "46-60"), (61, 10_000, "60+")]


def _payspeed_label(days: int) -> str:
    for lo, hi, label in _PAYSPEED_BUCKETS:
        if lo <= days <= hi:
            return label
    return "60+"


def performance(months_back: int = 12, today: dt.date | None = None) -> dict:
    """Margins (revenue vs carrier cost, by currency) and collection speed (how
    long customers take to pay, on-time %), company-wide and per customer.

    Margin is booked only on loads that have BOTH a Shipper Amt and a Carrier
    Amount, so a load still missing its carrier invoice doesn't inflate the number.
    Amounts are pre-tax (HST excluded — it's pass-through, not margin).
    """
    gc = _client()
    today = today or dt.date.today()
    acc = gc.get_all_values(ACCOUNTS_TAB)
    h = acc[0]

    def idx(name):
        return h.index(name)

    i_load, i_cust, i_inv, i_sa, i_ca, i_due, i_pmt, i_cur, i_status = (
        idx("Load no"), idx("Customer Name"), idx("invoice date"), idx("Shipper Amt"),
        idx("Carrier Amount"), idx("Due date"), idx("PMT Revd"), idx("Currency"), idx("STATUS"))

    def blank_agg():
        return {"loads": 0, "revenue": 0.0, "cost": 0.0, "margin": 0.0}

    # company-wide margin, per currency
    margin = {"CAD": blank_agg(), "USD": blank_agg()}
    # month -> {margin_cad, margin_usd, revenue_cad, revenue_usd}
    mbuckets: dict[str, dict] = {}
    # collection speed, company-wide
    days_list: list[int] = []
    on_time_hits = 0
    on_time_total = 0
    speed_dist = {label: 0 for _, _, label in _PAYSPEED_BUCKETS}
    # per (customer, currency)
    cust: dict[tuple, dict] = {}

    def get(row, i):
        return row[i] if i < len(row) else ""

    for row in acc[1:]:
        load = str(get(row, i_load)).strip()
        if not re.search(r"\d", load):
            continue
        status = str(get(row, i_status)).strip().lower()
        if status == "load cancelled":
            continue
        cur = _cur(get(row, i_cur))
        if cur == "OTHER":
            continue
        customer = str(get(row, i_cust)).strip() or "—"
        sa = _num(get(row, i_sa))
        ca = _num(get(row, i_ca))
        inv_d = _date(get(row, i_inv))
        due_d = _date(get(row, i_due))
        pmt_d = _date(get(row, i_pmt))

        ckey = (customer, cur)
        c = cust.setdefault(ckey, {
            "customer": customer, "currency": cur,
            "loads": 0, "revenue": 0.0, "cost": 0.0, "margin": 0.0,
            "days_list": [], "on_time_hits": 0, "on_time_total": 0,
        })

        # margin — only when both sides are present
        if sa > 0 and ca > 0:
            m = sa - ca
            margin[cur]["loads"] += 1
            margin[cur]["revenue"] += sa
            margin[cur]["cost"] += ca
            margin[cur]["margin"] += m
            c["loads"] += 1
            c["revenue"] += sa
            c["cost"] += ca
            c["margin"] += m
            if inv_d:
                mk = f"{inv_d.year:04d}-{inv_d.month:02d}"
                b = mbuckets.setdefault(mk, {"margin_cad": 0.0, "margin_usd": 0.0,
                                             "revenue_cad": 0.0, "revenue_usd": 0.0})
                b[f"margin_{cur.lower()}"] += m
                b[f"revenue_{cur.lower()}"] += sa

        # collection speed — needs an invoice date and a real payment date
        if inv_d and pmt_d and pmt_d >= inv_d:
            d = (pmt_d - inv_d).days
            days_list.append(d)
            speed_dist[_payspeed_label(d)] += 1
            c["days_list"].append(d)
            if due_d:
                on_time_total += 1
                c["on_time_total"] += 1
                if pmt_d <= due_d:
                    on_time_hits += 1
                    c["on_time_hits"] += 1

    def finalize_margin(agg):
        rev = agg["revenue"]
        return {
            "loads": agg["loads"],
            "revenue": round(rev, 2),
            "cost": round(agg["cost"], 2),
            "margin": round(agg["margin"], 2),
            "avg_margin": round(agg["margin"] / agg["loads"], 2) if agg["loads"] else 0.0,
            "margin_pct": round(agg["margin"] / rev * 100, 1) if rev else 0.0,
        }

    def med(xs):
        if not xs:
            return 0.0
        s = sorted(xs)
        n = len(s)
        return float(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)

    # per-customer rows
    cust_rows = []
    for c in cust.values():
        if c["loads"] == 0 and not c["days_list"]:
            continue
        rev = c["revenue"]
        cust_rows.append({
            "customer": c["customer"],
            "currency": c["currency"],
            "loads": c["loads"],
            "revenue": round(rev, 2),
            "margin": round(c["margin"], 2),
            "margin_pct": round(c["margin"] / rev * 100, 1) if rev else 0.0,
            "avg_days_to_pay": round(sum(c["days_list"]) / len(c["days_list"]), 1) if c["days_list"] else None,
            "on_time_pct": round(c["on_time_hits"] / c["on_time_total"] * 100, 0) if c["on_time_total"] else None,
            "paid_count": len(c["days_list"]),
        })
    cust_rows.sort(key=lambda r: r["revenue"], reverse=True)

    # monthly margin trend
    mkeys = sorted(mbuckets)[-months_back:]
    margin_months = []
    for k in mkeys:
        b = mbuckets[k]
        y, mm = k.split("-")
        margin_months.append({
            "month": k, "label": dt.date(int(y), int(mm), 1).strftime("%b %Y"),
            "margin_cad": round(b["margin_cad"], 2), "margin_usd": round(b["margin_usd"], 2),
            "revenue_cad": round(b["revenue_cad"], 2), "revenue_usd": round(b["revenue_usd"], 2),
        })

    return {
        "margin": {"CAD": finalize_margin(margin["CAD"]), "USD": finalize_margin(margin["USD"])},
        "margin_months": margin_months,
        "payment": {
            "avg_days_to_pay": round(sum(days_list) / len(days_list), 1) if days_list else 0.0,
            "median_days": med(days_list),
            "on_time_pct": round(on_time_hits / on_time_total * 100, 0) if on_time_total else 0.0,
            "paid_count": len(days_list),
            "distribution": speed_dist,
        },
        "customers": cust_rows,
        "as_of": today.isoformat(),
    }
