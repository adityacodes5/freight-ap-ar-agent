"""Audit harness — a READ-ONLY health check of the agent's accounting writes.

Two passes, no writes and no emails sent:

  1. CONSISTENCY — for every load the AI has a cached reading for
     (agent_extractions), compare what's on the live Accounts sheet against what
     the AI actually read. Flags carrier invoice# and amount mismatches — this is
     what caught the load#-as-invoice# corruption (5693 → 1229125, 5485 → 5485A).
     Fast, no AI calls.

  2. BACKLOG — re-run BOTH lanes in DRY-RUN over the last N emails (no writes, no
     sends) and report every decision plus the extracted-vs-sheet comparison, with
     anomalies flagged (discrepancy / hold / review / no-row / missing invoice#).
     Uses the extraction cache, so already-read emails cost no tokens.

Writes a markdown report (default audit_report.md) and prints a summary.

    python audit.py                  # consistency + backlog over last 150 emails
    python audit.py --limit 300      # wider backlog (more tokens for uncached)
    python audit.py --no-backlog     # consistency only (fast, zero AI)
"""
import argparse
import sys

from dotenv import load_dotenv
from loguru import logger

load_dotenv()


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (ValueError, AttributeError):
        return 0.0


# ── Pass 1: sheet vs the AI's cached reading ──────────────────────────────────
def consistency_pass() -> list[dict]:
    from config import Config
    from tools.sheets import GoogleSheetsClient
    from db import USE_DB, db_execute

    if not USE_DB:
        logger.warning("DATABASE_URL not set — skipping the consistency pass (needs the extraction cache).")
        return []

    rows = db_execute(
        """select distinct on (data->>'load_number')
                  data->>'load_number'   as load,
                  data->>'invoice_number' as inv,
                  data->>'carrier_name'  as carrier,
                  data->>'carrier_amount' as amount
           from agent_extractions
           where coalesce(data->>'load_number','') <> ''
           order by data->>'load_number', cached_at desc""",
        fetch="all") or []
    ai = {r[0]: {"inv": r[1], "carrier": r[2], "amount": r[3]} for r in rows}

    cfg = Config()
    sheets = GoogleSheetsClient(cfg)
    recs = sheets.get_all_records(cfg.accounts_sheet_name)
    issues = []
    for i, r in enumerate(recs):
        ln = str(r.get("Load no", "")).strip()
        if not ln or ln not in ai:
            continue
        a = ai[ln]
        row = i + 2
        sheet_inv = str(r.get("Carrier In#", "")).strip()
        sheet_amt = _num(r.get("Carrier Amount", ""))

        # Invoice# — flag when the AI read a real number that the sheet doesn't have.
        real_inv = (a["inv"] or "").strip()
        if real_inv and sheet_inv and real_inv != sheet_inv:
            sev = "HIGH" if sheet_inv == ln else "check"
            issues.append({"load": ln, "row": row, "field": "Carrier In#",
                           "sheet": sheet_inv, "ai": real_inv, "sev": sev})

        # Amount — flag a real dollar gap.
        ai_amt = _num(a["amount"])
        if ai_amt and sheet_amt and abs(ai_amt - sheet_amt) > 1.0:
            issues.append({"load": ln, "row": row, "field": "Carrier Amount",
                           "sheet": f"{sheet_amt:g}", "ai": f"{ai_amt:g}", "sev": "check"})
    return issues


# ── Pass 2: dry-run both lanes over the recent backlog ────────────────────────
def backlog_pass(limit: int) -> dict:
    from run_batch import run_batch
    from run_carrier_batch import run_carrier_batch

    out = {}
    for lane, fn in (("carrier", run_carrier_batch), ("shipper", run_batch)):
        stats = fn(limit=limit, dry_run=True)
        out[lane] = stats.get("_report_items", [])
    return out


# anomaly = anything a human should look at. no_load/no_row matter most here:
# no_load = couldn't find OUR load number (the digging gap); no_row = load not in
# the sheet yet.
_CARRIER_FLAG = {"discrepancy", "error", "no_load", "no_row"}
_SHIPPER_FLAG = {"hold", "review", "error", "no_row"}


def _flagged(lane: str, item: dict) -> bool:
    st = item.get("status", "")
    if lane == "carrier":
        if st in _CARRIER_FLAG:
            return True
        # verified but no invoice number captured
        if st == "verified" and not str((item.get("details") or {}).get("invoice_number", "")).strip():
            return True
    else:
        if st in _SHIPPER_FLAG:
            return True
    return False


def _md_table(lane: str, items: list[dict]) -> str:
    flagged = [it for it in items if _flagged(lane, it)]
    lines = [f"### {lane.title()} — {len(items)} processed, **{len(flagged)} flagged**\n"]
    if not flagged:
        lines.append("_No anomalies._\n")
        return "\n".join(lines)
    lines.append("| Load | Status | Detail | Subject |")
    lines.append("|---|---|---|---|")
    for it in flagged:
        d = it.get("details") or {}
        if lane == "carrier" and d:
            detail = (f"inv={d.get('invoice_number','')!r} "
                      f"carrier='{d.get('carrier_invoice','')}' vs sheet='{d.get('carrier_sheet','')}' "
                      f"${d.get('amount_invoice','')}/{d.get('amount_sheet','')}")
        else:
            detail = it.get("reason", "")
        subj = (it.get("email_subject", "") or "")[:40]
        lines.append(f"| {it.get('load_no','')} | {it.get('status','')} | {detail[:90]} | {subj} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=150, help="emails per lane for the backlog pass")
    ap.add_argument("--no-backlog", action="store_true", help="consistency pass only (no AI)")
    ap.add_argument("--out", default="audit_report.md")
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")

    md = ["# Agent audit report\n"]

    logger.info("Pass 1/2 — sheet vs cached AI readings (consistency)…")
    issues = consistency_pass()
    high = [x for x in issues if x["sev"] == "HIGH"]
    md.append(f"## 1. Consistency (sheet vs AI) — {len(issues)} mismatch(es), {len(high)} high-severity\n")
    if issues:
        md.append("| Sev | Load | Row | Field | Sheet has | AI read |")
        md.append("|---|---|---|---|---|---|")
        for x in sorted(issues, key=lambda y: (y["sev"] != "HIGH", y["load"])):
            md.append(f"| {x['sev']} | {x['load']} | {x['row']} | {x['field']} | `{x['sheet']}` | `{x['ai']}` |")
    else:
        md.append("_No mismatches against cached readings._")
    md.append("")

    if not args.no_backlog:
        logger.info("Pass 2/2 — dry-run backlog over last {} emails per lane (no writes)…", args.limit)
        bl = backlog_pass(args.limit)
        md.append(f"## 2. Backlog decisions (dry-run, last {args.limit})\n")
        for lane in ("carrier", "shipper"):
            md.append(_md_table(lane, bl.get(lane, [])))
            md.append("")

    report = "\n".join(md)
    with open(args.out, "w") as f:
        f.write(report)

    # console summary
    logger.info("─" * 52)
    logger.info("Consistency: {} mismatch(es) ({} HIGH).", len(issues), len(high))
    for x in high:
        logger.info("  HIGH  load {} row {}  {}: sheet={!r} ai={!r}",
                    x["load"], x["row"], x["field"], x["sheet"], x["ai"])
    logger.info("Full report → {}", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
