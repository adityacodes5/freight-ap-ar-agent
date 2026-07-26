"""
Run reports and the review queue.

Backend mirrors state.py:
  • DATABASE_URL set  → Postgres (tables agent_run_reports, agent_review_queue).
                        Durable across Render redeploys, and shared between the
                        web service and the scheduled cron process — so a cron
                        run's results show up in the dashboard.
  • DATABASE_URL unset → JSON files (run_reports.json / review_queue.json).

The public API is identical in both modes, so api/main.py and the runners never
change:
  - RunReport.add / counts / finish
  - list_reports
  - add_review_item / list_review / resolve_review_item / clear_resolved

A "review item" is any load the agent flagged: a discrepancy, a payment hold, a
low-confidence match, or a hard error. The dashboard reads these to show what
needs human eyes.
"""

import json
import os
from datetime import datetime

from db import USE_DB, db_execute

_REPORTS_FILE = os.path.join(os.path.dirname(__file__), "run_reports.json")
_REVIEW_FILE  = os.path.join(os.path.dirname(__file__), "review_queue.json")

MAX_REPORTS = 20

# statuses that should land in the review queue for a human
_FLAGGED = ("discrepancy", "error", "no_load", "hold", "review")


# ── low-level JSON IO (fallback) ──────────────────────────────────────────────

def _load(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _save(path: str, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _json(data):
    """Wrap a dict/list as jsonb for psycopg."""
    from psycopg.types.json import Json
    return Json(data)


# ── Run reports ───────────────────────────────────────────────────────────────

class RunReport:
    """Accumulates per-load outcomes during one batch run, then persists."""

    def __init__(self, run_type: str):
        self.run_type = run_type  # "shipper" | "carrier"
        self.run_id = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self.started_at = datetime.now().isoformat()
        self.items: list[dict] = []

    def add(
        self,
        load_no: str,
        status: str,          # verified | discrepancy | no_load | no_row | error | hold | skipped_done
        reason: str = "",
        sheet_row: int | None = None,
        email_from: str = "",
        email_subject: str = "",
        message_id: str = "",
        forwarded: bool | None = None,
        details: dict | None = None,
    ) -> None:
        self.items.append({
            "load_no": load_no,
            "status": status,
            "reason": reason,
            "sheet_row": sheet_row,
            "email_from": email_from,
            "email_subject": email_subject,
            "message_id": message_id,
            "forwarded": forwarded,
            "details": details or {},
            "at": datetime.now().isoformat(),
        })

    def counts(self) -> dict:
        c = {"verified": 0, "discrepancy": 0, "no_load": 0, "no_row": 0,
             "error": 0, "hold": 0, "skipped_done": 0, "forwarded": 0}
        for it in self.items:
            c[it["status"]] = c.get(it["status"], 0) + 1
            if it.get("forwarded"):
                c["forwarded"] += 1
        return c

    def finish(self) -> dict:
        """Persist this run report and sync flagged items into the review queue."""
        report = {
            "run_id": self.run_id,
            "type": self.run_type,
            "started_at": self.started_at,
            "finished_at": datetime.now().isoformat(),
            "counts": self.counts(),
            "items": self.items,
        }

        if USE_DB:
            db_execute(
                """insert into agent_run_reports
                     (run_id, type, started_at, finished_at, counts, items)
                   values (%s, %s, %s, %s, %s, %s)""",
                (report["run_id"], report["type"], report["started_at"],
                 report["finished_at"], _json(report["counts"]), _json(report["items"])))
            # keep only the most recent MAX_REPORTS
            db_execute(
                """delete from agent_run_reports
                   where id not in (
                     select id from agent_run_reports
                     order by finished_at desc limit %s)""",
                (MAX_REPORTS,))
        else:
            reports = _load(_REPORTS_FILE, [])
            reports.insert(0, report)
            _save(_REPORTS_FILE, reports[:MAX_REPORTS])

        for it in self.items:
            if it["status"] in _FLAGGED:
                add_review_item(
                    load_no=it["load_no"],
                    run_type=self.run_type,
                    status=it["status"],
                    reason=it["reason"],
                    sheet_row=it["sheet_row"],
                    email_from=it["email_from"],
                    email_subject=it["email_subject"],
                    message_id=it["message_id"],
                    details=it["details"],
                )
        return report


def list_reports() -> list[dict]:
    if USE_DB:
        rows = db_execute(
            """select run_id, type, started_at, finished_at, counts, items
               from agent_run_reports order by finished_at desc limit %s""",
            (MAX_REPORTS,), fetch="all") or []
        return [
            {"run_id": r[0], "type": r[1],
             "started_at": r[2].isoformat() if r[2] else None,
             "finished_at": r[3].isoformat() if r[3] else None,
             "counts": r[4] or {}, "items": r[5] or []}
            for r in rows
        ]
    return _load(_REPORTS_FILE, [])


# ── Review queue ──────────────────────────────────────────────────────────────

def _review_key(load_no: str, run_type: str) -> str:
    return f"{load_no or '?'}::{run_type}"


def add_review_item(
    load_no: str,
    run_type: str,
    status: str,
    reason: str = "",
    sheet_row: int | None = None,
    email_from: str = "",
    email_subject: str = "",
    message_id: str = "",
    details: dict | None = None,
) -> None:
    key = _review_key(load_no, run_type)
    now = datetime.now().isoformat()
    if USE_DB:
        # re-flagging an item reopens it (resolved → false) and refreshes fields,
        # preserving the original created_at
        db_execute(
            """insert into agent_review_queue
                 (key, load_no, type, status, reason, sheet_row, email_from,
                  email_subject, message_id, details, created_at, updated_at, resolved)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now(), now(), false)
               on conflict (key) do update set
                 status        = excluded.status,
                 reason        = excluded.reason,
                 sheet_row     = excluded.sheet_row,
                 email_from    = excluded.email_from,
                 email_subject = excluded.email_subject,
                 message_id    = excluded.message_id,
                 details       = excluded.details,
                 updated_at    = now(),
                 resolved      = false,
                 resolved_at   = null""",
            (key, load_no, run_type, status, reason, sheet_row, email_from,
             email_subject, message_id, _json(details or {})))
    else:
        queue = _load(_REVIEW_FILE, {})
        existing = queue.get(key, {})
        queue[key] = {
            "load_no": load_no, "type": run_type, "status": status,
            "reason": reason, "sheet_row": sheet_row, "email_from": email_from,
            "email_subject": email_subject, "message_id": message_id,
            "details": details or {},
            "created_at": existing.get("created_at", now),
            "updated_at": now, "resolved": False,
        }
        _save(_REVIEW_FILE, queue)


def list_review(include_resolved: bool = False) -> list[dict]:
    if USE_DB:
        where = "" if include_resolved else "where not resolved"
        rows = db_execute(
            f"""select key, load_no, type, status, reason, sheet_row, email_from,
                       email_subject, message_id, details, created_at, updated_at,
                       resolved, resolved_at
                from agent_review_queue {where} order by updated_at desc""",
            fetch="all") or []
        return [
            {"key": r[0], "load_no": r[1], "type": r[2], "status": r[3],
             "reason": r[4], "sheet_row": r[5], "email_from": r[6],
             "email_subject": r[7], "message_id": r[8], "details": r[9] or {},
             "created_at": r[10].isoformat() if r[10] else None,
             "updated_at": r[11].isoformat() if r[11] else None,
             "resolved": r[12], "resolved_at": r[13].isoformat() if r[13] else None}
            for r in rows
        ]
    queue = _load(_REVIEW_FILE, {})
    items = [{"key": k, **v} for k, v in queue.items()]
    if not include_resolved:
        items = [i for i in items if not i.get("resolved")]
    items.sort(key=lambda i: i.get("updated_at", ""), reverse=True)
    return items


def resolve_review_item(key: str) -> bool:
    if USE_DB:
        res = db_execute(
            "update agent_review_queue set resolved=true, resolved_at=now() where key=%s",
            (key,))
        # db_execute returns None; treat as success if the row exists
        row = db_execute("select 1 from agent_review_queue where key=%s", (key,), fetch="one")
        return row is not None
    queue = _load(_REVIEW_FILE, {})
    if key in queue:
        queue[key]["resolved"] = True
        queue[key]["resolved_at"] = datetime.now().isoformat()
        _save(_REVIEW_FILE, queue)
        return True
    return False


def clear_resolved() -> int:
    if USE_DB:
        before = db_execute("select count(*) from agent_review_queue where resolved",
                            fetch="one")
        db_execute("delete from agent_review_queue where resolved")
        return int(before[0]) if before else 0
    queue = _load(_REVIEW_FILE, {})
    before = len(queue)
    queue = {k: v for k, v in queue.items() if not v.get("resolved")}
    _save(_REVIEW_FILE, queue)
    return before - len(queue)
