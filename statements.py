"""Monthly cash statement — a bank-style statement of money in and out for one
calendar month, built purely from the Accounts workbook.

  • CREDITS  = customer payments received   (the "Payment Received" tab)
  • DEBITS   = carrier payments made        (the "ACH/Check" tab, by date mailed)
  • MARGIN   = shipper − carrier on loads invoiced that month (Accounts)

CAD and USD are kept strictly separate (no FX). Returns structured data plus a
professional HTML render suitable for emailing or attaching.
"""
import calendar
import datetime as dt

from analytics import _client, _cur, _date, _num, ACCOUNTS_TAB

PAYMENTS_TAB = "Payment Received"
ACH_TAB = "ACH/Check"


def _blank():
    return {"credits": 0.0, "debits": 0.0, "credit_count": 0, "debit_count": 0}


def statement(year: int, month: int) -> dict:
    """Cash statement for the given calendar month."""
    gc = _client()
    first = dt.date(year, month, 1)
    last = dt.date(year, month, calendar.monthrange(year, month)[1])

    cur = {"CAD": _blank(), "USD": _blank()}
    inflows, outflows = [], []

    # CREDITS — Payment Received: TOTAL(3) or Amount(1), Currency(4), Date(6), Shipper(7), Invoice(0)
    for r in gc.get_all_values(PAYMENTS_TAB)[1:]:
        d = _date(r[6]) if len(r) > 6 else None
        if not d or not (first <= d <= last):
            continue
        c = _cur(r[4]) if len(r) > 4 else "OTHER"
        if c not in cur:
            continue
        amt = _num(r[3]) if len(r) > 3 and _num(r[3]) else (_num(r[1]) if len(r) > 1 else 0)
        if amt <= 0:
            continue
        cur[c]["credits"] += amt
        cur[c]["credit_count"] += 1
        inflows.append({"date": d.isoformat(),
                        "ref": (r[0] if len(r) > 0 else "").strip(),
                        "party": (r[7] if len(r) > 7 else "").strip() or "Customer",
                        "amount": round(amt, 2), "currency": c})

    # DEBITS — ACH/Check: Load(0), Carrier(1), Fact.Co(3), Amount(6), currency(7), date mailed(9)
    for r in gc.get_all_values(ACH_TAB)[1:]:
        d = _date(r[9]) if len(r) > 9 else None
        if not d or not (first <= d <= last):
            continue
        c = _cur(r[7]) if len(r) > 7 else "OTHER"
        if c not in cur:
            continue
        amt = _num(r[6]) if len(r) > 6 else 0
        if amt <= 0:
            continue
        # Payee = Carrier name (col 1). Col 3 ("Fact.Co.") is a yes/no factored flag
        # on the ACH sheet, NOT a company name — never use it as the description.
        payee = (r[1] if len(r) > 1 else "").strip() or "Carrier"
        cur[c]["debits"] += amt
        cur[c]["debit_count"] += 1
        outflows.append({"date": d.isoformat(),
                         "ref": (r[0] if len(r) > 0 else "").strip(),
                         "party": payee, "amount": round(amt, 2), "currency": c})

    # MARGIN — Accounts loads invoiced in the month (both shipper & carrier present)
    margin = {"CAD": {"revenue": 0.0, "cost": 0.0, "margin": 0.0, "loads": 0},
              "USD": {"revenue": 0.0, "cost": 0.0, "margin": 0.0, "loads": 0}}
    acc = gc.get_all_values(ACCOUNTS_TAB)
    h = acc[0]
    ix = {n: h.index(n) for n in ("invoice date", "Shipper Amt", "Carrier Amount", "Currency", "STATUS")}
    for r in acc[1:]:
        d = _date(r[ix["invoice date"]]) if ix["invoice date"] < len(r) else None
        if not d or not (first <= d <= last):
            continue
        if (r[ix["STATUS"]] if ix["STATUS"] < len(r) else "").strip().lower() == "load cancelled":
            continue
        c = _cur(r[ix["Currency"]]) if ix["Currency"] < len(r) else "OTHER"
        if c not in margin:
            continue
        sa = _num(r[ix["Shipper Amt"]]) if ix["Shipper Amt"] < len(r) else 0
        ca = _num(r[ix["Carrier Amount"]]) if ix["Carrier Amount"] < len(r) else 0
        if sa > 0 and ca > 0:
            margin[c]["revenue"] += sa
            margin[c]["cost"] += ca
            margin[c]["margin"] += sa - ca
            margin[c]["loads"] += 1

    for c in cur:
        cur[c]["credits"] = round(cur[c]["credits"], 2)
        cur[c]["debits"] = round(cur[c]["debits"], 2)
        cur[c]["net"] = round(cur[c]["credits"] - cur[c]["debits"], 2)
    for c in margin:
        rev = margin[c]["revenue"]
        margin[c] = {"revenue": round(rev, 2), "cost": round(margin[c]["cost"], 2),
                     "margin": round(margin[c]["margin"], 2), "loads": margin[c]["loads"],
                     "pct": round(margin[c]["margin"] / rev * 100, 1) if rev else 0.0}

    inflows.sort(key=lambda x: x["date"])
    outflows.sort(key=lambda x: x["date"])
    return {
        "period": {"year": year, "month": month,
                   "label": first.strftime("%B %Y"),
                   "start": first.isoformat(), "end": last.isoformat()},
        "cur": cur, "margin": margin,
        "inflows": inflows, "outflows": outflows,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


# ── Professional HTML render ──────────────────────────────────────────────────
def _m(n: float) -> str:
    return f"{n:,.2f}"


def _txn_table(title: str, rows: list, kind: str, currency: str) -> str:
    """One currency's transaction list. Lean markup so the whole statement stays
    under Gmail's ~102KB clip limit even with several hundred rows."""
    total = round(sum(r["amount"] for r in rows), 2)
    col = "Credit" if kind == "credit" else "Debit"
    h = [f"<div style='font-weight:700;font-size:13px;color:#1a3c5e;margin:14px 0 3px'>{title}</div>"]
    h.append("<table cellpadding='5' cellspacing='0' style='width:100%;border-collapse:collapse;font-size:12px;color:#33454f'>")
    h.append(
        "<tr style='background:#1a3c5e;color:#fff'>"
        "<td align='left'>Date</td><td align='left'>Description</td>"
        f"<td align='left'>Ref</td><td align='right'>{col}</td></tr>")
    for i, r in enumerate(rows):
        stripe = " bgcolor='#f4f8fb'" if i % 2 else ""
        h.append(
            f"<tr{stripe}><td>{r['date'][5:]}</td><td>{_esc(r['party'])[:42]}</td>"
            f"<td>{_esc(r['ref'])[:12]}</td><td align='right'>{_m(r['amount'])}</td></tr>")
    h.append(
        "<tr style='border-top:2px solid #1a3c5e;font-weight:700'>"
        f"<td colspan='3'>Total {col.lower()}s — {currency}</td>"
        f"<td align='right'>{currency} {_m(total)}</td></tr>")
    return "".join(h) + "</table>"


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_statement_html(data: dict, company: str = "Demo Logistics") -> str:
    p = data["period"]
    h = ["<div style='font-family:Arial,Helvetica,sans-serif;color:#20303f;max-width:760px;margin:0 auto'>"]

    # Letterhead
    h.append(
        "<div style='border-bottom:3px solid #1a3c5e;padding:0 0 12px;margin-bottom:10px'>"
        f"<div style='font-size:24px;font-weight:800;color:#1a3c5e;letter-spacing:.5px'>{_esc(company)}</div>"
        "<div style='display:flex;justify-content:space-between'>"
        "<span style='font-size:14px;color:#5a6b7b'>Monthly Cash Statement</span>"
        f"<span style='font-size:13px;color:#5a6b7b'>{_esc(p['label'])}</span></div></div>")
    h.append(
        f"<div style='font-size:12px;color:#7a8794;margin-bottom:16px'>"
        f"Statement period {p['start']} to {p['end']} &nbsp;·&nbsp; generated {data['generated_at'][:10]}"
        "<br>Credits = customer payments received &nbsp;·&nbsp; Debits = carrier payments issued &nbsp;·&nbsp; CAD and USD reported separately.</div>")

    for c in ("CAD", "USD"):
        cs = data["cur"][c]
        mg = data["margin"][c]
        net = cs["net"]
        net_col = "#1a7a3c" if net >= 0 else "#b3261e"
        h.append(f"<div style='margin:0 0 8px;border:1px solid #d5dde5;border-radius:8px;overflow:hidden'>")
        h.append(
            f"<div style='background:#eef3f8;padding:10px 14px;border-bottom:1px solid #d5dde5;"
            f"font-weight:800;font-size:16px;color:#1a3c5e'>{c} Account "
            f"<span style='font-weight:normal;font-size:12px;color:#7a8794'>({cs['credit_count']} credits · {cs['debit_count']} debits)</span></div>")
        # Summary row
        h.append("<table cellpadding='0' cellspacing='0' style='width:100%;border-collapse:collapse'>")
        h.append(
            "<tr>"
            f"<td style='padding:12px 14px;width:33%'><div style='font-size:11px;color:#7a8794;text-transform:uppercase'>Total credits (in)</div>"
            f"<div style='font-size:19px;font-weight:700;color:#1a7a3c'>{c} {_m(cs['credits'])}</div></td>"
            f"<td style='padding:12px 14px;width:33%;border-left:1px solid #eee'><div style='font-size:11px;color:#7a8794;text-transform:uppercase'>Total debits (out)</div>"
            f"<div style='font-size:19px;font-weight:700;color:#b3261e'>{c} {_m(cs['debits'])}</div></td>"
            f"<td style='padding:12px 14px;width:34%;border-left:1px solid #eee'><div style='font-size:11px;color:#7a8794;text-transform:uppercase'>Net movement</div>"
            f"<div style='font-size:19px;font-weight:800;color:{net_col}'>{c} {_m(net)}</div></td></tr>")
        h.append("</table>")
        # Margin strip
        h.append(
            f"<div style='background:#fbfcfe;border-top:1px solid #eee;padding:8px 14px;font-size:12px;color:#42586b'>"
            f"<b>Gross margin (loads invoiced {p['label']}):</b> {c} {_m(mg['margin'])} "
            f"on {c} {_m(mg['revenue'])} revenue &nbsp;·&nbsp; <b>{mg['pct']}%</b> &nbsp;·&nbsp; {mg['loads']} loads</div>")
        h.append("</div>")

    # Combined margin summary
    tot_m = data["margin"]
    h.append(
        "<div style='margin:16px 0;padding:12px 14px;background:#1a3c5e;color:#fff;border-radius:8px'>"
        f"<div style='font-size:12px;text-transform:uppercase;letter-spacing:.5px;opacity:.85'>Total gross margin — {_esc(p['label'])} operations</div>"
        f"<div style='font-size:18px;font-weight:800;margin-top:2px'>CAD {_m(tot_m['CAD']['margin'])} &nbsp;+&nbsp; USD {_m(tot_m['USD']['margin'])}</div></div>")

    # Transaction detail per currency
    for c in ("CAD", "USD"):
        ins = [x for x in data["inflows"] if x["currency"] == c]
        outs = [x for x in data["outflows"] if x["currency"] == c]
        if not ins and not outs:
            continue
        h.append(f"<h3 style='color:#1a3c5e;border-bottom:1px solid #d5dde5;padding-bottom:4px;margin:22px 0 2px'>{c} — transaction detail</h3>")
        if ins:
            h.append(_txn_table(f"Credits received ({len(ins)})", ins, "credit", c))
        if outs:
            h.append(_txn_table(f"Debits paid ({len(outs)})", outs, "debit", c))

    h.append(
        "<div style='margin-top:22px;padding-top:10px;border-top:1px solid #d5dde5;font-size:11px;color:#9aa7b3'>"
        "This statement is generated automatically from the DEMO Accounts workbook. Figures reflect payments dated within the "
        "statement period. Not a bank document.</div></div>")
    return "".join(h)


def statement_html(year: int, month: int) -> tuple[str, str]:
    """Convenience: (subject, html) for a month."""
    data = statement(year, month)
    html = render_statement_html(data)
    subj = f"Demo — {data['period']['label']} Cash Statement"
    return subj, html
