"""Auto-reconcile the review queue against the live sheet before we notify.

The team often FIXES a flagged load directly in Google Sheets — corrects the
amount, sets STATUS to Verified/Complete, or clears the magenta highlight — but
forgets to click "mark done" on the dashboard. Those items then nag in the daily
email forever. So right before sending, we re-check every open item against the
sheet and auto-resolve the ones that are clearly handled, and we drop any of the
CURRENT run's flags that are already fine on the sheet (the watermark re-scans
recent emails, so it can re-flag a load you fixed last week).

Resolution signals (conservative — only clear on clear positive evidence):
  • no_row  → the load now EXISTS on the Accounts sheet.
  • any     → STATUS is now Verified / Complete / Load Cancelled (a human touched it).
  • discrepancy → the row is no longer highlighted magenta (the flag was cleared).
"""
import re

from loguru import logger

from config import Config
from review import list_review, resolve_review_item
from tools.sheets import GoogleSheetsClient

ACCOUNTS_TAB = "Accounts"
# STATUS values that mean a human has dealt with the row.
_DONE_STATUS = {"verified", "complete", "load cancelled"}


def _accounts_index(gc) -> dict:
    """load number → [{row, status}] for every Accounts row (1-based row incl. header)."""
    acc = gc.get_all_values(ACCOUNTS_TAB)
    h = acc[0]
    il, istatus = h.index("Load no"), h.index("STATUS")
    idx: dict[str, list] = {}
    for n, r in enumerate(acc[1:], start=2):
        m = re.search(r"\d+", r[il] if il < len(r) else "")
        if not m:
            continue
        idx.setdefault(m.group(), []).append(
            {"row": n, "status": (r[istatus] if istatus < len(r) else "").strip().lower()})
    return idx


def _is_cleared_white(bg: dict) -> bool:
    """True only if the row highlight is GONE — an empty/default or white cell.
    Google omits zero channels, so a magenta flag {red:1,green:0,blue:1} arrives as
    {red:1,blue:1}; a red/orange team flag is also non-white. We treat only a truly
    white/blank cell as 'the human cleared the flag' — never another colour."""
    if not bg:
        return True
    return all(bg.get(ch, 0) >= 0.9 for ch in ("red", "green", "blue"))


def _resolution(item: dict, idx: dict, gc, bg_cache: dict) -> str | None:
    """Short reason if this item now looks handled on the sheet, else None."""
    status = (item.get("status") or "").lower()
    if status == "no_load":
        return None  # junk mail with no load number — nothing on the sheet to check
    m = re.search(r"\d+", str(item.get("load_no") or ""))
    if not m:
        return None
    rows = idx.get(m.group())

    if status == "no_row":
        return "load is now on the sheet" if rows else None
    if not rows:
        return None
    done = next((rr["status"] for rr in rows if rr["status"] in _DONE_STATUS), None)
    if done:
        return f"STATUS is now '{done}'"
    if status == "discrepancy":
        try:
            row = int(item.get("sheet_row") or rows[0]["row"])
        except (TypeError, ValueError):
            return None
        if row not in bg_cache:
            got = gc.column_backgrounds(ACCOUNTS_TAB, "A", row, row)
            bg_cache[row] = got[0] if got else {}
        if _is_cleared_white(bg_cache[row]):
            return "discrepancy flag cleared on the sheet"
    return None


def reconcile_open_items(gc=None) -> dict:
    """Auto-resolve every open review item that now looks handled on the sheet.
    Returns the cleared items plus the shared Accounts index / colour cache so the
    caller can reuse them to filter the current run's flags too."""
    gc = gc or GoogleSheetsClient(Config())
    idx = _accounts_index(gc)
    bg_cache: dict = {}
    cleared = []
    for it in list_review():
        why = _resolution(it, idx, gc, bg_cache)
        if why and resolve_review_item(it["key"]):
            cleared.append({"load_no": it["load_no"], "status": it["status"], "why": why})
            logger.info("Auto-resolved {} ({}) — {}", it["load_no"], it["status"], why)
    return {"cleared": cleared, "cleared_count": len(cleared), "index": idx, "bg_cache": bg_cache}


def resolved_report_loads(report_items: list, idx: dict, gc, bg_cache: dict) -> set:
    """Load numbers among the CURRENT run's flags that are already fine on the sheet
    — so the notifier can drop them instead of nagging."""
    out = set()
    for it in report_items or []:
        if _resolution(it, idx, gc, bg_cache):
            m = re.search(r"\d+", str(it.get("load_no") or ""))
            if m:
                out.add(m.group())
    return out
