"""
Operational state for the invoice agent: which (message_id, attachment_id)
pairs are already processed, the AI extraction cache, and the forward-to-dispatch
retry queue.

Backend is chosen at runtime:
  • DATABASE_URL set  → Postgres (Supabase). Survives Render redeploys, so the
                        extraction cache and processed/forward state are durable
                        and re-runs are idempotent (no re-burning the AI API,
                        no double sheet writes, no double dispatch forwards).
  • DATABASE_URL unset → JSON files in the project root (local dev fallback).

The public API is identical in both modes, so call sites never change.
"""

import json
import os
from datetime import datetime

from loguru import logger

from db import USE_DB, db_execute as _db_execute  # shared Postgres backend

_STATE_FILE = os.path.join(os.path.dirname(__file__), "processed_invoices.json")
_FWD_FILE   = os.path.join(os.path.dirname(__file__), "forward_pending.json")
_FWDLOAD_FILE = os.path.join(os.path.dirname(__file__), "forwarded_loads.json")
_CACHE_FILE = os.path.join(os.path.dirname(__file__), "extraction_cache.json")


def _key(message_id: str, attachment_id: str) -> str:
    return f"{message_id}::{attachment_id}"


# ════════════════════════════════════════════════════════════════════════════
# JSON fallback backend
# ════════════════════════════════════════════════════════════════════════════
def _json_load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not read {} — starting fresh", os.path.basename(path))
        return {}


def _json_save(path: str, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ════════════════════════════════════════════════════════════════════════════
# Processed-invoices tracking
# ════════════════════════════════════════════════════════════════════════════
def is_processed(message_id: str, attachment_id: str) -> bool:
    """Return True if this attachment has already been processed."""
    if USE_DB:
        row = _db_execute(
            "select 1 from agent_processed_invoices where message_id=%s and attachment_id=%s",
            (message_id, attachment_id), fetch="one")
        return row is not None
    return _key(message_id, attachment_id) in _json_load(_STATE_FILE)


def mark_processed(
    message_id: str,
    attachment_id: str,
    invoice_number: str = "",
    carrier_name: str = "",
    sheet_row: int | None = None,
) -> None:
    """Record that this attachment was successfully processed (idempotent upsert)."""
    if USE_DB:
        _db_execute(
            """insert into agent_processed_invoices
                 (message_id, attachment_id, invoice_number, carrier_name, sheet_row, processed_at)
               values (%s, %s, %s, %s, %s, now())
               on conflict (message_id, attachment_id) do update set
                 invoice_number = excluded.invoice_number,
                 carrier_name   = excluded.carrier_name,
                 sheet_row      = excluded.sheet_row,
                 processed_at   = now()""",
            (message_id, attachment_id, invoice_number or "", carrier_name or "", sheet_row))
    else:
        state = _json_load(_STATE_FILE)
        state[_key(message_id, attachment_id)] = {
            "processed_at": datetime.now().isoformat(),
            "invoice_number": invoice_number,
            "carrier_name": carrier_name,
            "sheet_row": sheet_row,
        }
        _json_save(_STATE_FILE, state)
    logger.debug("Marked processed: {} / {} → invoice={}", message_id, attachment_id, invoice_number)


def list_processed() -> list[dict]:
    """Return all processed entries (for debugging)."""
    if USE_DB:
        rows = _db_execute(
            """select message_id, attachment_id, invoice_number, carrier_name,
                      sheet_row, processed_at
               from agent_processed_invoices order by processed_at desc""", fetch="all") or []
        return [
            {"key": _key(r[0], r[1]), "invoice_number": r[2], "carrier_name": r[3],
             "sheet_row": r[4], "processed_at": r[5].isoformat() if r[5] else None}
            for r in rows
        ]
    return [{"key": k, **v} for k, v in _json_load(_STATE_FILE).items()]


# ════════════════════════════════════════════════════════════════════════════
# Forward-pending queue
# ════════════════════════════════════════════════════════════════════════════
# Emails whose sheet update succeeded but whose forward-to-dispatch failed.
# Retried at the start of every run, independently of processed/read state.
def add_forward_pending(message_id: str, load_no: str, subject: str,
                        from_addr: str, recv_date: str) -> None:
    if USE_DB:
        _db_execute(
            """insert into agent_forward_pending
                 (message_id, load_no, subject, from_addr, recv_date, attempts, added_at)
               values (%s, %s, %s, %s, %s, 1, now())
               on conflict (message_id) do update set
                 load_no   = excluded.load_no,
                 subject   = excluded.subject,
                 from_addr = excluded.from_addr,
                 recv_date = excluded.recv_date,
                 attempts  = agent_forward_pending.attempts + 1,
                 added_at  = now()""",
            (message_id, load_no, subject, from_addr, recv_date))
    else:
        fwd = _json_load(_FWD_FILE)
        fwd[message_id] = {
            "load_no": load_no, "subject": subject, "from_addr": from_addr,
            "recv_date": recv_date, "added_at": datetime.now().isoformat(),
            "attempts": fwd.get(message_id, {}).get("attempts", 0) + 1,
        }
        _json_save(_FWD_FILE, fwd)


def list_forward_pending() -> list[dict]:
    if USE_DB:
        rows = _db_execute(
            """select message_id, load_no, subject, from_addr, recv_date, attempts, added_at
               from agent_forward_pending order by added_at""", fetch="all") or []
        return [
            {"message_id": r[0], "load_no": r[1], "subject": r[2], "from_addr": r[3],
             "recv_date": r[4], "attempts": r[5], "added_at": r[6].isoformat() if r[6] else None}
            for r in rows
        ]
    return [{"message_id": k, **v} for k, v in _json_load(_FWD_FILE).items()]


def remove_forward_pending(message_id: str) -> None:
    if USE_DB:
        _db_execute("delete from agent_forward_pending where message_id=%s", (message_id,))
    else:
        fwd = _json_load(_FWD_FILE)
        if message_id in fwd:
            del fwd[message_id]
            _json_save(_FWD_FILE, fwd)


# ════════════════════════════════════════════════════════════════════════════
# Per-load forward guard
# ════════════════════════════════════════════════════════════════════════════
# A load is forwarded to dispatch at most once, even when the carrier sends
# multiple emails for it (invoice + NOA + re-sends). The processed/forward state
# is keyed per (message, attachment), so without this guard each distinct email
# for the same load would forward again and spam dispatch.
def _norm_load(load_no) -> str:
    return str(load_no or "").strip()


def is_load_forwarded(load_no) -> bool:
    """Return True if this load number was already forwarded to dispatch."""
    key = _norm_load(load_no)
    if not key:
        return False
    if USE_DB:
        row = _db_execute(
            "select 1 from agent_forwarded_loads where load_no=%s", (key,), fetch="one")
        return row is not None
    return key in _json_load(_FWDLOAD_FILE)


def mark_load_forwarded(load_no, message_id: str = "") -> None:
    """Record that this load was forwarded to dispatch (idempotent)."""
    key = _norm_load(load_no)
    if not key:
        return
    if USE_DB:
        _db_execute(
            """insert into agent_forwarded_loads (load_no, message_id, forwarded_at)
               values (%s, %s, now())
               on conflict (load_no) do nothing""",
            (key, message_id or ""))
    else:
        fwd = _json_load(_FWDLOAD_FILE)
        if key not in fwd:
            fwd[key] = {"message_id": message_id, "forwarded_at": datetime.now().isoformat()}
            _json_save(_FWDLOAD_FILE, fwd)


# ════════════════════════════════════════════════════════════════════════════
# AI extraction cache
# ════════════════════════════════════════════════════════════════════════════
# Caches the expensive AI/vision reading of each PDF, keyed by message+attachment.
# A re-run reuses the cached reading instead of calling the API again, so emails
# that ended in no-row / no-load / discrepancy never burn vision twice. The cheap
# sheet-matching still re-runs every time (so a no-row load gets picked up once
# its row is added) — only the API call is skipped.
def get_cached_extraction(message_id: str, attachment_id: str) -> dict | None:
    """Return the cached AI reading for this PDF, or None if not cached yet."""
    if USE_DB:
        row = _db_execute(
            "select data from agent_extractions where message_id=%s and attachment_id=%s",
            (message_id, attachment_id), fetch="one")
        return row[0] if row else None
    entry = _json_load(_CACHE_FILE).get(_key(message_id, attachment_id))
    return entry.get("data") if entry else None


# Cap the cache so it can't grow without bound (keeps Postgres small). Only the
# most recent CACHE_MAX readings are kept; evicting an old one just means that
# (rarely re-touched) email would re-read once if it ever comes back.
CACHE_MAX = int(os.environ.get("CACHE_MAX", "500"))


def cache_extraction(message_id: str, attachment_id: str, data: dict) -> None:
    """Store the AI reading for this PDF so future runs skip the API call."""
    if USE_DB:
        from psycopg.types.json import Json
        _db_execute(
            """insert into agent_extractions (message_id, attachment_id, data, cached_at)
               values (%s, %s, %s, now())
               on conflict (message_id, attachment_id) do update set
                 data = excluded.data, cached_at = now()""",
            (message_id, attachment_id, Json(data)))
        # evict oldest beyond the cap
        _db_execute(
            """delete from agent_extractions where ctid not in (
                 select ctid from agent_extractions order by cached_at desc limit %s)""",
            (CACHE_MAX,))
    else:
        cache = _json_load(_CACHE_FILE)
        cache[_key(message_id, attachment_id)] = {
            "cached_at": datetime.now().isoformat(), "data": data,
        }
        # keep only the most recent CACHE_MAX entries by cached_at
        if len(cache) > CACHE_MAX:
            keep = sorted(cache.items(),
                          key=lambda kv: kv[1].get("cached_at", ""), reverse=True)[:CACHE_MAX]
            cache = dict(keep)
        _json_save(_CACHE_FILE, cache)
