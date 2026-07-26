"""Shared Postgres connection helper for durable agent state.

Backend is chosen at runtime by the presence of DATABASE_URL (the Supabase
connection-pooling string). Both state.py (cache / processed / forward / forwarded
loads) and review.py (run reports / review queue) import from here so there's a
single connection and a single reconnect policy.

If DATABASE_URL is unset, USE_DB is False and callers fall back to JSON files.
"""

import json
import os

from loguru import logger

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_DB = bool(DATABASE_URL)

_conn = None


def get_conn():
    """Lazily open (and cache) an autocommit Postgres connection, reconnecting
    if it has been dropped (Render/Supabase idle timeouts)."""
    global _conn
    import psycopg  # imported only when DATABASE_URL is set

    if _conn is not None and not _conn.closed:
        return _conn
    _conn = psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=10)
    return _conn


def db_execute(query: str, params: tuple = (), fetch: str | None = None):
    """Run a query, reconnecting once if the cached connection went stale.
    fetch: None | "one" | "all"."""
    global _conn
    for attempt in (1, 2):
        try:
            with get_conn().cursor() as cur:
                cur.execute(query, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return None
        except Exception as exc:  # noqa: BLE001 — reconnect on any conn error
            _conn = None
            if attempt == 2:
                logger.error("DB query failed after reconnect: {}", exc)
                raise
            logger.warning("DB connection lost — reconnecting ({})", exc)


# ── Cross-process run lock ────────────────────────────────────────────────────
# Stops a manual run and a scheduled run from processing the inbox at the same
# time. Table-based (pooler-safe, unlike session advisory locks). A stale lock
# older than the TTL is reclaimed so a crashed run can't block forever. When
# DATABASE_URL is unset (local dev, single machine) it's a no-op.

def acquire_run_lock(holder: str = "run", ttl_minutes: int = 20) -> bool:
    """Try to claim the batch run lock. Returns True if acquired."""
    if not USE_DB:
        return True
    row = db_execute(
        f"""insert into agent_run_lock (name, locked_at, holder)
            values ('batch', now(), %s)
            on conflict (name) do update
              set locked_at = now(), holder = excluded.holder
              where agent_run_lock.locked_at < now() - interval '{int(ttl_minutes)} minutes'
            returning name""",
        (holder,), fetch="one")
    return row is not None


def release_run_lock() -> None:
    if not USE_DB:
        return
    try:
        db_execute("delete from agent_run_lock where name='batch'")
    except Exception as exc:  # noqa: BLE001 — releasing must never raise
        logger.warning("Could not release run lock: {}", exc)


# ── Inbox watermark ───────────────────────────────────────────────────────────
# So a run processes EVERYTHING new since the last good run, not just a fixed
# "last N". We store the receivedTime (epoch ms) we'd scanned up to; the next run
# fetches everything newer (plus a lookback floor for retries). No DB → returns 0
# and callers fall back to the fixed-limit window.

def get_watermark(name: str = "inbox") -> int:
    if not USE_DB:
        return 0
    try:
        row = db_execute("select value from agent_watermark where name=%s", (name,), fetch="one")
        return int(row[0]) if row and row[0] else 0
    except Exception:  # noqa: BLE001
        return 0


def set_watermark(value: int, name: str = "inbox") -> None:
    if not USE_DB or not value:
        return
    try:
        db_execute(
            """insert into agent_watermark (name, value, updated_at) values (%s,%s,now())
               on conflict (name) do update set value=excluded.value, updated_at=now()""",
            (name, str(int(value))))
    except Exception as exc:  # noqa: BLE001 — watermark is best-effort
        logger.warning("Could not save watermark: {}", exc)


# ── Analytics cache ───────────────────────────────────────────────────────────
# The analytics views read the ENTIRE Accounts / Payment Received / ACH sheets and
# recompute from scratch — several seconds and a chunk of Sheets API quota per
# load. Since the underlying data only changes when a batch runs or a payment is
# recorded, we memoise each computed payload in Postgres and serve it until it's
# older than the caller's TTL. `refresh=True` (the dashboard's ↻ button) bypasses.
# No DB (local dev) → cache is a no-op and every call recomputes.

_cache_ready = False


def _ensure_cache_table() -> None:
    global _cache_ready
    if _cache_ready:
        return
    db_execute(
        """create table if not exists agent_analytics_cache (
               key text primary key,
               payload jsonb not null,
               computed_at timestamptz not null default now())""")
    _cache_ready = True


def cache_get(key: str, max_age_seconds: int):
    """Return (payload, age_seconds) if a fresh entry exists, else None."""
    if not USE_DB:
        return None
    try:
        _ensure_cache_table()
        row = db_execute(
            "select payload, extract(epoch from (now()-computed_at)) "
            "from agent_analytics_cache where key=%s",
            (key,), fetch="one")
        if not row:
            return None
        payload, age = row
        age = float(age or 0)
        if age > max_age_seconds:
            return None
        return payload, int(age)
    except Exception as exc:  # noqa: BLE001 — cache miss must never break a request
        logger.warning("analytics cache read failed ({}): {}", key, exc)
        return None


def cache_set(key: str, payload: dict) -> None:
    if not USE_DB:
        return
    try:
        _ensure_cache_table()
        db_execute(
            """insert into agent_analytics_cache (key, payload, computed_at)
               values (%s, %s::jsonb, now())
               on conflict (key) do update
                 set payload=excluded.payload, computed_at=now()""",
            (key, json.dumps(payload)))
    except Exception as exc:  # noqa: BLE001 — caching is best-effort
        logger.warning("analytics cache write failed ({}): {}", key, exc)


def cache_invalidate(prefix: str = "") -> None:
    """Drop cached analytics (all, or those whose key starts with `prefix`).
    Call after a batch run or a recorded payment so the next load is fresh."""
    if not USE_DB:
        return
    try:
        if prefix:
            db_execute("delete from agent_analytics_cache where key like %s", (prefix + "%",))
        else:
            db_execute("delete from agent_analytics_cache")
    except Exception as exc:  # noqa: BLE001
        logger.warning("analytics cache invalidate failed: {}", exc)
