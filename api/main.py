"""
Demo Logistics — FastAPI backend (runs on Render)
Exposes the batch runner over HTTP with SSE log streaming.
"""

import asyncio
import atexit
import base64
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.security.api_key import APIKeyHeader

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

# ── Google service-account JSON from base64 env var ───────────────────────────
_sa_b64 = os.environ.get("GOOGLE_SERVICE_ACCOUNT_B64", "")
if _sa_b64:
    _sa_json = base64.b64decode(_sa_b64).decode()
    _tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    _tmp.write(_sa_json)
    _tmp.close()
    # Hardening: the private key must persist on disk while the process runs
    # (downstream libs need GOOGLE_SERVICE_ACCOUNT_FILE to be a real path), so we
    # can't delete=True. Instead we (1) force owner-only 0600 perms — NamedTemporaryFile
    # is usually 0600 but make it explicit and guaranteed — and (2) register an atexit
    # hook to unlink the key file on clean shutdown so it isn't left world-persistent.
    try:
        os.chmod(_tmp.name, 0o600)
    except OSError:
        pass

    def _cleanup_sa_file(p=_tmp.name):
        try:
            if os.path.exists(p):
                os.unlink(p)
        except OSError:
            pass  # never let shutdown crash over cleanup

    atexit.register(_cleanup_sa_file)
    os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"] = _tmp.name

# Add repo root to path so run_batch imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from run_batch import run_batch  # noqa: E402
from run_carrier_batch import run_carrier_batch  # noqa: E402
import review as review_store  # noqa: E402
import review_chat  # noqa: E402
import prompts as prompt_store  # noqa: E402
from db import acquire_run_lock, release_run_lock  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from typing import Optional  # noqa: E402

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="Demo Invoice API")

ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── Auth: Google sign-in via Supabase, locked to a single email ───────────────
# The dashboard signs the user in with Google through Supabase Auth and sends the
# resulting access token as `Authorization: Bearer <token>` (or ?access_token= for
# SSE, which can't set headers). We validate the token against Supabase and only
# allow ALLOWED_EMAIL through.
import requests as _requests  # noqa: E402

ALLOWED_EMAILS    = {e.strip().lower() for e in os.environ.get(
    "ALLOWED_EMAIL", "owner@demologistics.com,ap.clerk@demologistics.com").split(",") if e.strip()}
SUPABASE_URL      = os.environ.get("SUPABASE_URL", "https://yourproject.supabase.co").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "sb_publishable_REPLACE_ME")

bearer_header = APIKeyHeader(name="Authorization", auto_error=False)
# Service-to-service key so project 2's backend can proxy to this API (its users
# authenticate to project 2 with its own JWT; project 2 → this API uses the key).
service_header = APIKeyHeader(name="X-Service-Key", auto_error=False)
AGENT_SERVICE_KEY = os.environ.get("AGENT_SERVICE_KEY", "").strip()
_token_cache: dict[str, tuple[str, float]] = {}  # token -> (email, expires_at)


def _is_service(x_service_key: str | None) -> bool:
    return bool(AGENT_SERVICE_KEY) and x_service_key == AGENT_SERVICE_KEY


def _verify_token(raw: str | None) -> str:
    """Validate a Supabase access token and return the user's email, or raise.
    Enforces the single-email allowlist."""
    token = (raw or "").removeprefix("Bearer ").removeprefix("bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Sign in required")

    now = time.time()
    cached = _token_cache.get(token)
    if cached and cached[1] > now:
        email = cached[0]
    else:
        try:
            r = _requests.get(
                f"{SUPABASE_URL}/auth/v1/user",
                headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {token}"},
                timeout=10,
            )
        except Exception:
            raise HTTPException(status_code=503, detail="Auth service unreachable")
        if r.status_code != 200:
            raise HTTPException(status_code=401, detail="Invalid or expired session")
        data = r.json()
        email = (data.get("email") or "").strip().lower()
        verified = bool(data.get("email_confirmed_at") or data.get("confirmed_at"))
        if not email or not verified:
            raise HTTPException(status_code=403, detail="Email not verified")
        # Cache for 60s to avoid per-call Supabase round-trips. Safe: the cache is
        # keyed by the exact (Supabase-validated) token and stores only the email it
        # resolved to, so it can never authenticate a different identity.
        _token_cache[token] = (email, now + 60)

    if email not in ALLOWED_EMAILS:
        raise HTTPException(status_code=403, detail="Not authorized for this account")
    return email


def require_key(
    authorization: str = Security(bearer_header),
    x_service_key: str = Security(service_header),
):
    # Header-only: the JWT must arrive via the Authorization header, never a query
    # string. Query-token auth is deliberately confined to /api/stream (SSE), where
    # EventSource cannot set headers — keeping JWTs out of the URLs of the normal
    # API surface (and therefore out of server/proxy access logs).
    if _is_service(x_service_key):
        return
    _verify_token(authorization)


def current_email(
    authorization: str = Security(bearer_header),
    x_service_key: str = Security(service_header),
) -> str:
    """Like require_key, but returns the verified user's email (for audit fields).
    Header-only (see require_key) — no query-string token accepted here."""
    if _is_service(x_service_key):
        return "automation@demo.internal"
    return _verify_token(authorization)


# ── In-memory job state (shared — only one job at a time) ────────────────────
_job: dict = {
    "running": False,
    "job_type": None,   # "shipper" | "carrier"
    "logs": [],
    "stats": None,
    "started_at": None,
    "finished_at": None,
    "error": None,
}
_job_lock = threading.Lock()


def _invalidate_analytics() -> None:
    """Drop the memoised analytics after a live write so the next dashboard load
    recomputes. Best-effort — never let it break the request that triggered it."""
    try:
        from db import cache_invalidate
        cache_invalidate()
    except Exception:  # noqa: BLE001
        pass


def _resolve_scan(limit: Optional[int]) -> tuple[bool, int, str]:
    """Turn an optional user-supplied email count into a scan mode.

    Returns (use_watermark, cap, mode_label):
      • limit is None  → WATERMARK mode: each lane scans everything since its own
        last run and advances its watermark. Default; identical to the cron.
      • limit is a number → FIXED mode: scan the last N emails (no watermark).
    """
    if limit is None:
        return True, 500, "since last run"
    return False, limit, f"last {limit} emails"


def _combine_stats(results: dict) -> dict:
    """Sum the numeric counters across lanes into one dict for the status cards."""
    out: dict = {}
    for st in results.values():
        if not isinstance(st, dict):
            continue
        for k, v in st.items():
            if isinstance(v, (int, float)):
                out[k] = out.get(k, 0) + v
    return out


def _make_thread(fn, limit: int, dry_run: bool, job_type: str, use_watermark: bool = False):
    def _run():
        with _job_lock:
            _job.update(
                running=True,
                job_type=job_type,
                logs=[],
                stats=None,
                error=None,
                started_at=datetime.now().isoformat(),
                finished_at=None,
            )

        def on_log(msg: str) -> None:
            with _job_lock:
                _job["logs"].append(msg)

        try:
            stats = fn(limit=limit, dry_run=dry_run, progress_callback=on_log,
                       use_watermark=use_watermark)
            with _job_lock:
                _job["stats"] = stats
            if not dry_run:
                _invalidate_analytics()
        except Exception as exc:
            with _job_lock:
                _job["error"] = str(exc)
                _job["logs"].append(f"ERROR: {exc}")
        finally:
            release_run_lock()
            with _job_lock:
                _job["running"] = False
                _job["finished_at"] = datetime.now().isoformat()

    return threading.Thread(target=_run, daemon=True)


def _make_all_thread(dry_run: bool, use_watermark: bool, mode_label: str):
    """RUN ALL: SALES→ACCOUNTS sync, then shipper, then carrier — same sequence as
    the daily cron. Each lane advances its OWN watermark, so the next run (manual or
    cron) starts where this one finished."""
    def _run():
        with _job_lock:
            _job.update(running=True, job_type="all", logs=[], stats=None, error=None,
                        started_at=datetime.now().isoformat(), finished_at=None)

        def on_log(msg: str) -> None:
            with _job_lock:
                _job["logs"].append(msg)

        results: dict = {}
        try:
            on_log(f"══ RUN ALL — scanning {mode_label}, both lanes ══")

            on_log("── SALES → ACCOUNTS sync ──")
            try:
                from sync_sales_to_accounts import sync
                res = sync(commit=not dry_run)
                on_log(f"Accounts sync: {res['message']}")
            except Exception as exc:
                on_log(f"Accounts sync FAILED: {exc}")

            for name, fn in (("shipper", run_batch), ("carrier", run_carrier_batch)):
                on_log(f"── {name.upper()} ──")
                try:
                    results[name] = fn(limit=500, dry_run=dry_run,
                                       progress_callback=on_log, use_watermark=use_watermark)
                except Exception as exc:
                    results[name] = {"_error": str(exc)}
                    on_log(f"{name.upper()} run FAILED: {exc}")

            with _job_lock:
                _job["stats"] = _combine_stats(results)
            if not dry_run:
                _invalidate_analytics()
            on_log("══ RUN ALL complete ══")
        except Exception as exc:
            with _job_lock:
                _job["error"] = str(exc)
                _job["logs"].append(f"ERROR: {exc}")
        finally:
            release_run_lock()
            with _job_lock:
                _job["running"] = False
                _job["finished_at"] = datetime.now().isoformat()

    return threading.Thread(target=_run, daemon=True)


def _start_job(fn, limit: int, dry_run: bool, job_type: str, use_watermark: bool = False) -> dict:
    """Guard against a concurrent run (in-memory + cross-process DB lock), then
    spawn the worker thread. The thread releases the DB lock in its finally."""
    with _job_lock:
        if _job["running"]:
            raise HTTPException(status_code=409, detail="A batch is already running")
    if not acquire_run_lock(holder=job_type):
        raise HTTPException(status_code=409,
                            detail="A scheduled run is in progress — try again shortly")
    _make_thread(fn, limit, dry_run, job_type, use_watermark).start()
    return {"started": True, "type": job_type, "limit": limit}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "ts": datetime.now().isoformat()}


@app.post("/api/run")
def trigger_shipper(limit: Optional[int] = None, dry_run: bool = False, _: None = Security(require_key)):
    """Shipper lane. Default (no limit) = everything new since the last run
    (watermark). Pass ?limit=N to force scanning the last N emails instead."""
    use_wm, cap, _label = _resolve_scan(limit)
    return _start_job(run_batch, cap, dry_run, "shipper", use_wm)


@app.post("/api/run-carrier")
def trigger_carrier(limit: Optional[int] = None, dry_run: bool = False, _: None = Security(require_key)):
    """Carrier lane. Default (no limit) = everything new since the last run
    (watermark). Pass ?limit=N to force scanning the last N emails instead."""
    use_wm, cap, _label = _resolve_scan(limit)
    return _start_job(run_carrier_batch, cap, dry_run, "carrier", use_wm)


@app.post("/api/run-all")
def trigger_all(limit: Optional[int] = None, dry_run: bool = False, _: None = Security(require_key)):
    """RUN ALL: SALES→ACCOUNTS sync + shipper + carrier in one go — the same
    sequence the daily cron runs. Default = everything new since the last run."""
    with _job_lock:
        if _job["running"]:
            raise HTTPException(status_code=409, detail="A batch is already running")
    if not acquire_run_lock(holder="all"):
        raise HTTPException(status_code=409,
                            detail="A scheduled run is in progress — try again shortly")
    use_wm, _cap, mode_label = _resolve_scan(limit)
    _make_all_thread(dry_run, use_wm, mode_label).start()
    return {"started": True, "type": "all", "mode": mode_label}


@app.post("/api/sync-sales")
def sync_sales(commit: bool = False, _: None = Security(require_key)):
    """Copy new loads from the India-team SALES sheet into ACCOUNTS.
    Defaults to a dry-run preview; pass ?commit=true to actually write."""
    from sync_sales_to_accounts import sync
    try:
        return sync(commit=commit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/sync-ach")
def sync_ach_endpoint(commit: bool = False, through: str | None = None,
                      _: None = Security(require_key)):
    """Populate the ACH/Check payment sheet from Accounts (due-to-pay loads).
    Manual, web-only — deliberately NOT part of Run All or the cron, because
    payments go out weekly and adding loads daily mid-verification is confusing.
    `through` = pay loads due on or before this date (YYYY-MM-DD) plus overdue;
    defaults to today. Defaults to a dry-run preview; pass ?commit=true to write."""
    from sync_accounts_to_ach import sync_ach
    try:
        result = sync_ach(commit=commit, through=through)
        if commit:
            _invalidate_analytics()
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# Analytics rescans whole sheets, so results are memoised in Postgres and served
# until this many seconds old. The dashboard's ↻ button sends ?refresh=true.
_ANALYTICS_TTL = 900  # 15 minutes


@app.get("/api/analytics/cashflow")
def analytics_cashflow(refresh: bool = False, _: None = Security(require_key)):
    """Monthly inflow (payments received) vs outflow (carrier payments), by currency."""
    from analytics import cached, cashflow
    try:
        return cached("cashflow", _ANALYTICS_TTL, cashflow, refresh=refresh)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/analytics/overdue")
def analytics_overdue(refresh: bool = False, _: None = Security(require_key)):
    """Receivables owed to us and past due, with aging + top debtors."""
    from analytics import cached, overdue
    try:
        return cached("overdue", _ANALYTICS_TTL, overdue, refresh=refresh)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/analytics/payables")
def analytics_payables(refresh: bool = False, _: None = Security(require_key)):
    """Carrier invoices past due that we haven't paid yet, with aging + top payees."""
    from analytics import cached, overdue_payables
    try:
        return cached("payables", _ANALYTICS_TTL, overdue_payables, refresh=refresh)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/load/{load_no}")
def load_lookup_endpoint(load_no: str, _: None = Security(require_key)):
    """Full money lifecycle of one load (Accounts + Payment Received + ACH/Check)."""
    from lookup import load_lookup
    try:
        return load_lookup(load_no)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/today")
def today(_: None = Security(require_key)):
    """Home 'what needs you' rollup: open review backlog, payables due this week,
    overdue receivables, and the last run — each a jump-off point."""
    from analytics import cached, overdue, payables_snapshot
    out: dict = {"as_of": datetime.now().date().isoformat()}
    try:
        from review import list_review
        items = list_review()
        out["review"] = {"count": len(items),
                         "items": [{"load_no": i["load_no"], "type": i["type"],
                                    "status": i["status"], "reason": (i.get("reason") or "")[:140],
                                    "key": i["key"]} for i in items[:6]]}
    except Exception as exc:  # noqa: BLE001
        out["review"] = {"count": 0, "items": [], "error": str(exc)}
    try:
        pay = cached("payables_snapshot", _ANALYTICS_TTL, payables_snapshot)
        out["payables"] = {"overdue": pay["overdue"], "due_soon": pay["due_soon"]}
    except Exception as exc:  # noqa: BLE001
        out["payables"] = {"error": str(exc)}
    try:
        ar = cached("overdue", _ANALYTICS_TTL, overdue)
        out["receivables"] = {"count": ar["count"], "totals": ar["totals"]}
    except Exception as exc:  # noqa: BLE001
        out["receivables"] = {"error": str(exc)}
    try:
        from review import list_reports
        reps = list_reports()
        out["last_run"] = reps[0] if reps else None
    except Exception as exc:  # noqa: BLE001
        out["last_run"] = None
    out["run_health"] = _run_health(out.get("last_run"))
    return out


# The scheduled cron (render.yaml) fires once a day at 22:00 UTC (~6pm Toronto).
_CRON_UTC_HOUR = 22
_RUN_GRACE = timedelta(minutes=45)  # how long a run may take before we call it late


def _run_health(last_run: dict | None) -> dict:
    """A plain-language health read on the scheduled agent: did the last run happen
    on time, did it error, and when is the next one due. So a quiet dashboard can be
    told apart from a dead cron. All times UTC-aware; the client renders them local.
    (`finished_at` is stored naive == server UTC on Render, so we treat it as UTC.)"""
    now = datetime.now(timezone.utc)
    today_run = now.replace(hour=_CRON_UTC_HOUR, minute=0, second=0, microsecond=0)
    next_run = today_run if today_run > now else today_run + timedelta(days=1)
    # the most recent run that should already have finished by now
    last_expected = today_run if now >= today_run + _RUN_GRACE else today_run - timedelta(days=1)

    last_finished = None
    errors = 0
    if last_run:
        errors = int((last_run.get("counts") or {}).get("error", 0) or 0)
        fin = last_run.get("finished_at")
        if fin:
            try:
                dt = datetime.fromisoformat(fin)
                last_finished = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                pass

    stale = last_finished is None or last_finished < last_expected
    status = "stale" if stale else ("errors" if errors > 0 else "ok")
    return {
        "status": status,  # ok | errors | stale
        "errors": errors,
        "last_finished": last_finished.isoformat() if last_finished else None,
        "next_run": next_run.isoformat(),
        "schedule_label": "daily · around 6pm Toronto",
    }


@app.get("/api/analytics/performance")
def analytics_performance(refresh: bool = False, _: None = Security(require_key)):
    """Margins (revenue vs carrier cost) and collection speed, company-wide + per customer."""
    from analytics import cached, performance
    try:
        return cached("performance", _ANALYTICS_TTL, performance, refresh=refresh)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/analytics/carriers")
def analytics_carriers(refresh: bool = False, _: None = Security(require_key)):
    """Per-carrier scorecard: loads, spend, margin we earn, and issue rate."""
    from analytics import cached, carrier_scorecard
    try:
        return cached("carriers", _ANALYTICS_TTL, carrier_scorecard, refresh=refresh)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def _statement_period(year: Optional[int], month: Optional[int]) -> tuple[int, int]:
    """Default to last completed month when not specified."""
    if year and month:
        return year, month
    from datetime import date
    t = date.today()
    return (t.year, t.month - 1) if t.month > 1 else (t.year - 1, 12)


@app.get("/api/statement/download")
def statement_download(year: Optional[int] = None, month: Optional[int] = None,
                       _: None = Security(require_key)):
    """Generate the month's bank-style PDF statement and return it for download."""
    from statement_pdf import build_with_outstanding
    y, m = _statement_period(year, month)
    try:
        pdf, fname, _data = build_with_outstanding(y, m)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/api/statement/email")
def statement_email(year: Optional[int] = None, month: Optional[int] = None,
                    to: Optional[str] = None, _: None = Security(require_key)):
    """Email the month's PDF statement (defaults to SUMMARY_EMAIL)."""
    from statement_pdf import send_statement_email
    y, m = _statement_period(year, month)
    dest = to or os.environ.get("SUMMARY_EMAIL", "owner@demologistics.com")
    try:
        ok = send_statement_email(y, m, dest)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=502, detail="Statement email failed to send")
    return {"sent": True, "year": y, "month": m, "to": dest}


@app.get("/api/receivables/index")
def receivables_index(_: None = Security(require_key)):
    """Per-load expected invoice figures (amount, HST, currency, paid?) so the
    cash-application UI can validate a cheque split against the sheet."""
    from receivables import index
    try:
        return index()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class ReceivableLine(BaseModel):
    load: str
    amount: float
    currency: str | None = None


class ApplyBody(BaseModel):
    date: str
    method: str = "EFT"
    customer: str = ""
    reference: str = ""
    lines: list[ReceivableLine]


@app.post("/api/receivables/apply")
def receivables_apply(body: ApplyBody, _: None = Security(require_key)):
    """Record a cheque split: append one Payment Received row per load. Accounts'
    PMT Revd auto-updates via VLOOKUP, marking each load paid."""
    from receivables import apply
    lines = [{"load": ln.load, "amount": ln.amount, "currency": ln.currency} for ln in body.lines]
    try:
        result = apply(date=body.date, method=body.method, lines=lines,
                       customer=body.customer, reference=body.reference)
        _invalidate_analytics()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class UndoBody(BaseModel):
    rows: list[int]


@app.post("/api/receivables/undo")
def receivables_undo(body: UndoBody, _: None = Security(require_key)):
    """Reverse a just-recorded payment: reset its Payment Received row(s) to the
    blank formula template, so Accounts marks the load unpaid again."""
    from receivables import undo
    try:
        result = undo(body.rows)
        _invalidate_analytics()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/status")
def get_status(_: None = Security(require_key)):
    with _job_lock:
        return {
            "running": _job["running"],
            "started_at": _job["started_at"],
            "finished_at": _job["finished_at"],
            "stats": _job["stats"],
            "error": _job["error"],
            "log_lines": len(_job["logs"]),
        }


# ── Run history + review queue ────────────────────────────────────────────────

@app.get("/api/runs")
def get_runs(_: None = Security(require_key)):
    return {"runs": review_store.list_reports()}


@app.get("/api/review")
def get_review(_: None = Security(require_key)):
    return {"items": review_store.list_review()}


@app.post("/api/review/resolve")
def resolve_item(key: str, _: None = Security(require_key)):
    ok = review_store.resolve_review_item(key)
    return {"resolved": ok}


# ── Editable AI prompts (shared by both products) ─────────────────────────────

@app.get("/api/prompts")
def get_prompts(_: None = Security(require_key)):
    """All editable prompts (key, content, description, who/when last changed)."""
    return {"prompts": prompt_store.list_prompts()}


class PromptBody(BaseModel):
    key: str
    content: str


@app.post("/api/prompts")
def update_prompt(body: PromptBody, email: str = Security(current_email)):
    """Edit a prompt. Takes effect for BOTH products + the cron within ~20s
    (every process reads the same agent_prompts table)."""
    if not body.key.strip() or not body.content.strip():
        raise HTTPException(status_code=400, detail="key and content are required")
    prompt_store.set_prompt(body.key.strip(), body.content, updated_by=email)
    return {"ok": True, "key": body.key.strip(), "updated_by": email}


class ChatBody(BaseModel):
    messages: list[dict]


@app.post("/api/review/chat")
def review_chat_endpoint(body: ChatBody, _: None = Security(require_key)):
    try:
        result = review_chat.chat(body.messages)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/stream")
async def stream_logs(
    authorization: str = Security(bearer_header),
    query_token: str = Query(None, alias="access_token"),
    x_service_key: str = Security(service_header),
):
    """SSE endpoint — streams log lines as they arrive.

    This is the ONLY endpoint that accepts the token via ?access_token=: browser
    EventSource cannot set an Authorization header, so the query token is functionally
    required here. Residual risk: SSE URLs (with the JWT) may land in server/proxy
    access logs. The rest of the API is header-only (see require_key/current_email)."""
    if not _is_service(x_service_key):
        _verify_token(authorization or (f"Bearer {query_token}" if query_token else None))

    async def generate():
        sent = 0
        # Keep streaming until job finishes and all logs sent
        while True:
            with _job_lock:
                current_logs = _job["logs"][sent:]
                running = _job["running"]
                stats = _job["stats"]
                error = _job["error"]

            for line in current_logs:
                sent += 1
                payload = json.dumps({"type": "log", "line": line})
                yield f"data: {payload}\n\n"

            if not running:
                # Send final result
                payload = json.dumps({"type": "done", "stats": stats, "error": error})
                yield f"data: {payload}\n\n"
                break

            # Heartbeat so Render/Vercel don't close the connection
            yield ": heartbeat\n\n"
            await asyncio.sleep(0.4)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
