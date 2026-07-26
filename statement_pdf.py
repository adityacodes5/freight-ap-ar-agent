"""Bank-style monthly statement as a real PDF (pure-Python fpdf2 — runs on Render,
no system libraries).

Black-and-white, LaTeX/booktabs typographic style like a real bank statement:
Times serif, horizontal rules only (no vertical lines, no shading), accounting
negatives in parentheses. One document, both sides: credits = customer payments
received (AR), debits = carrier payments issued (AP), in a running-balance ledger,
CAD and USD each on their own ledger, with a summary + margin page up front.
"""
from fpdf import FPDF

from statements import statement

USABLE = 186  # A4 portrait width minus 12mm margins
LEDGER = [20, 78, 18, 23, 23, 24]  # Date, Description, Ref, Debit, Credit, Balance


def _lat(s) -> str:
    """Core PDF fonts are latin-1 only — drop anything outside it."""
    return str(s or "").encode("latin-1", "replace").decode("latin-1")


def _m(n: float) -> str:
    """Accounting format: thousands separators, negatives in parentheses."""
    s = f"{abs(n):,.2f}"
    return f"({s})" if n < -0.001 else s


class _Statement(FPDF):
    company = "DEMO LOGISTICS"
    period = ""
    gen = ""
    section = ""
    col_ledger = False

    def _rule(self, thick: float = 0.4, gap: float = 0.0):
        if gap:
            self.ln(gap)
        self.set_line_width(thick)
        y = self.get_y()
        self.line(self.l_margin, y, self.l_margin + USABLE, y)
        self.ln(0.6)

    def header(self):
        self.set_text_color(0, 0, 0)
        self.set_y(12)
        self.set_font("Times", "B", 18)
        self.cell(120, 8, self.company)
        self.set_font("Times", "", 11)
        self.set_xy(self.l_margin + 66, 13)
        self.cell(120, 7, "ACCOUNT STATEMENT", align="R")
        self.set_y(21)
        self._rule(0.5)
        self.set_font("Times", "I", 9)
        self.cell(120, 5, _lat(self.period))
        self.cell(66, 5, _lat(f"Generated {self.gen}"), align="R")
        self.ln(7)
        if self.section:
            self.set_font("Times", "B", 12)
            self.cell(0, 6, _lat(self.section))
            self.ln(7)
        if self.col_ledger:
            self._ledger_head()

    def _ledger_head(self):
        self._rule(0.5)
        self.set_font("Times", "B", 9)
        labels = ["Date", "Description", "Ref", "Debit", "Credit", "Balance"]
        for i, (lab, w) in enumerate(zip(labels, LEDGER)):
            self.cell(w, 6, lab, align="R" if i >= 3 else "L")
        self.ln(6)
        self._rule(0.2)

    def footer(self):
        self.set_y(-12)
        self.set_font("Times", "I", 8)
        self.cell(0, 5, _lat(f"Demo Logistics  |  Page {self.page_no()} of {{nb}}"), align="C")


def _table(pdf: _Statement, widths: list, header: list, rows: list,
           aligns: list, total: list | None = None):
    """A booktabs-style table: top rule, bold header, mid rule, rows, bottom rule."""
    pdf._rule(0.5)
    pdf.set_font("Times", "B", 9.5)
    for lab, w, a in zip(header, widths, aligns):
        pdf.cell(w, 6, _lat(lab), align=a)
    pdf.ln(6)
    pdf._rule(0.2)
    pdf.set_font("Times", "", 9.5)
    for row in rows:
        for val, w, a in zip(row, widths, aligns):
            pdf.cell(w, 5.6, _lat(val), align=a)
        pdf.ln(5.6)
    if total:
        pdf._rule(0.2)
        pdf.set_font("Times", "B", 9.5)
        for val, w, a in zip(total, widths, aligns):
            pdf.cell(w, 6, _lat(val), align=a)
        pdf.ln(6)
    pdf._rule(0.5)
    pdf.ln(4)


def _ledger(pdf: _Statement, cur: str, entries: list):
    pdf.section = f"{cur} Ledger  -  Credits (customer receipts / AR) and Debits (carrier payments / AP)"
    pdf.col_ledger = True
    pdf.add_page()
    pdf.set_font("Times", "", 9)
    bal = credits = debits = 0.0
    for e in entries:
        is_credit = e["kind"] == "C"
        bal += e["amount"] if is_credit else -e["amount"]
        credits += e["amount"] if is_credit else 0
        debits += 0 if is_credit else e["amount"]
        row = [e["date"][5:], _lat(e["party"])[:48], _lat(e["ref"])[:11],
               "" if is_credit else _m(e["amount"]),
               _m(e["amount"]) if is_credit else "", _m(bal)]
        for j, (val, w) in enumerate(zip(row, LEDGER)):
            pdf.cell(w, 5.4, val, align="R" if j >= 3 else "L")
        pdf.ln(5.4)
    pdf._rule(0.2)
    pdf.set_font("Times", "B", 9.5)
    pdf.cell(LEDGER[0] + LEDGER[1] + LEDGER[2], 6, "Period totals")
    pdf.cell(LEDGER[3], 6, _m(debits), align="R")
    pdf.cell(LEDGER[4], 6, _m(credits), align="R")
    pdf.cell(LEDGER[5], 6, _m(bal), align="R")
    pdf.ln(6)
    pdf._rule(0.5)
    pdf.col_ledger = False
    pdf.section = ""


def build_statement_pdf(year: int, month: int, outstanding: dict | None = None) -> tuple[bytes, str, dict]:
    """Return (pdf_bytes, filename, data) for the month's bank-style statement."""
    data = statement(year, month)
    p = data["period"]
    cur, mg = data["cur"], data["margin"]

    pdf = _Statement(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(True, margin=16)
    pdf.set_title(f"Demo Statement {p['label']}")
    pdf.period = f"Statement period: {p['start']} to {p['end']}"
    pdf.gen = data["generated_at"][:10]

    pdf.section = "Statement Summary"
    pdf.add_page()

    # Cash summary
    _table(pdf, [46, 47, 47, 46],
           ["Account", "Credits (in)", "Debits (out)", "Net movement"],
           [[c, _m(cur[c]["credits"]), _m(cur[c]["debits"]), _m(cur[c]["net"])] for c in ("CAD", "USD")],
           ["L", "R", "R", "R"])

    # Margin summary
    pdf.set_font("Times", "B", 11)
    pdf.cell(0, 6, _lat("Gross Margin  (loads invoiced this month)"))
    pdf.ln(7)
    _table(pdf, [30, 39, 39, 39, 39],
           ["Account", "Revenue", "Carrier cost", "Gross margin", "Margin %"],
           [[c, _m(mg[c]["revenue"]), _m(mg[c]["cost"]), _m(mg[c]["margin"]), f"{mg[c]['pct']:.1f}%"]
            for c in ("CAD", "USD")],
           ["L", "R", "R", "R", "R"],
           total=["Total", _m(mg["CAD"]["revenue"] + mg["USD"]["revenue"]),
                  _m(mg["CAD"]["cost"] + mg["USD"]["cost"]),
                  _m(mg["CAD"]["margin"] + mg["USD"]["margin"]), ""])

    # Outstanding position
    if outstanding:
        ar, ap = outstanding.get("ar", {}), outstanding.get("ap", {})
        pdf.set_font("Times", "B", 11)
        pdf.cell(0, 6, _lat(f"Outstanding Position  (as of {pdf.gen})"))
        pdf.ln(7)
        _table(pdf, [62, 62, 62],
               ["", "CAD", "USD"],
               [["Receivables overdue (owed to us)", _m(ar.get("CAD", 0)), _m(ar.get("USD", 0))],
                ["Payables overdue (we owe carriers)", _m(ap.get("CAD", 0)), _m(ap.get("USD", 0))]],
               ["L", "R", "R"])

    pdf.set_font("Times", "I", 8.5)
    pdf.ln(1)
    pdf.multi_cell(USABLE, 5, _lat(
        "Credits are customer payments received (accounts receivable). Debits are carrier payments "
        "issued (accounts payable). CAD and USD are reported separately; no currency conversion is applied. "
        "Figures reflect payments dated within the statement period. This document is generated automatically "
        "from the DEMO Accounts records and is not a bank-issued statement."))

    for c in ("CAD", "USD"):
        entries = ([{**x, "kind": "C"} for x in data["inflows"] if x["currency"] == c]
                   + [{**x, "kind": "D"} for x in data["outflows"] if x["currency"] == c])
        entries.sort(key=lambda x: x["date"])
        if entries:
            _ledger(pdf, c, entries)

    pdf_bytes = bytes(pdf.output())
    fname = f"Demo_Statement_{p['year']}_{p['month']:02d}.pdf"
    return pdf_bytes, fname, data


def _prev_month(year: int, month: int) -> tuple[int, int]:
    return (year, month - 1) if month > 1 else (year - 1, 12)


def build_with_outstanding(year: int, month: int) -> tuple[bytes, str, dict]:
    """Build the PDF including the current outstanding AR/AP position (best-effort)."""
    outstanding = None
    try:
        from analytics import overdue, overdue_payables
        outstanding = {"ar": overdue()["totals"], "ap": overdue_payables()["totals"]}
    except Exception:  # noqa: BLE001 — outstanding is a nice-to-have
        pass
    return build_statement_pdf(year, month, outstanding=outstanding)


def send_statement_email(year: int, month: int, to: str, subject_prefix: str = "") -> bool:
    """Build the month's PDF and email it as an attachment."""
    from config import Config
    from tools.zoho_mail import ZohoMailClient

    pdf, fname, data = build_with_outstanding(year, month)
    p, cur, mg = data["period"], data["cur"], data["margin"]
    body = (
        "<div style='font-family:Arial,sans-serif;color:#20303f'>"
        f"<p>Attached is the <b>{p['label']} Cash Statement</b> (PDF).</p>"
        "<table style='font-size:14px'>"
        f"<tr><td>CAD net movement</td><td style='padding-left:16px'><b>${cur['CAD']['net']:,.2f}</b></td></tr>"
        f"<tr><td>USD net movement</td><td style='padding-left:16px'><b>${cur['USD']['net']:,.2f}</b></td></tr>"
        f"<tr><td>Gross margin</td><td style='padding-left:16px'>CAD ${mg['CAD']['margin']:,.0f} "
        f"({mg['CAD']['pct']}%) &middot; USD ${mg['USD']['margin']:,.0f} ({mg['USD']['pct']}%)</td></tr>"
        "</table><p style='color:#888;font-size:12px'>Generated automatically from the DEMO Accounts records.</p></div>")
    subject = f"{subject_prefix}Demo {p['label']} Cash Statement"
    return ZohoMailClient(Config()).send_email(to, subject, body, attachments=[(fname, pdf)])
