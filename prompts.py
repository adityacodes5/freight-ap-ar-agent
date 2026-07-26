"""
Editable AI prompts, stored in Postgres so they can be tuned from a dashboard
without a code change — and so a change made in ONE product (project 2's prompt
editor) is picked up by BOTH the original logistics-agent dashboard and the cron,
because every process reads the same `agent_prompts` table at runtime.

Backend mirrors db.py / review.py:
  • DATABASE_URL set  → Postgres table `agent_prompts` (key, content, …).
  • DATABASE_URL unset → in-memory defaults only (local dev).

The hardcoded prompt text still lives in the runner modules as the `default=`
fallback, so the agent keeps working even if the DB is unreachable or a key
hasn't been seeded yet. A short read-cache keeps a batch run from hammering the
DB while still letting edits propagate everywhere within `_TTL` seconds.

Public API:
  get_prompt(key, default="")   → current text (DB → cache → default)
  list_prompts()                → all rows for the editor UI
  set_prompt(key, content, by)  → upsert + bust local cache
"""
import time

from db import USE_DB, db_execute

_TTL = 20  # seconds — small, so a prompt edit shows up across processes quickly
_cache: dict[str, tuple[str, float]] = {}  # key -> (content, expires_at)


def get_prompt(key: str, default: str = "") -> str:
    """Return the current prompt text for *key*.

    Resolution order: fresh cache → DB → default. If the key is missing in the
    DB but a default is given, seed it so the editor shows the live text."""
    now = time.time()
    cached = _cache.get(key)
    if cached and cached[1] > now:
        return cached[0]

    content = default
    if USE_DB:
        try:
            row = db_execute("select content from agent_prompts where key=%s",
                             (key,), fetch="one")
            if row and row[0]:
                content = row[0]
            elif default:
                db_execute(
                    """insert into agent_prompts (key, content, description)
                       values (%s, %s, %s) on conflict (key) do nothing""",
                    (key, default, ""))
        except Exception:
            content = default  # DB hiccup → fall back to code default

    _cache[key] = (content, now + _TTL)
    return content


# Senders whose emails must NEVER be processed or forwarded (sensitive). Stored as
# an editable prompt (key "blocked_senders") so it shows up in the prompt editor,
# but enforced in code at the candidate/forward stages so it can't be missed.
DEFAULT_BLOCKED_SENDERS = """# Emails from these senders are NEVER processed or forwarded (sensitive).
# One match per line — an email address, a domain, or a name fragment (case-insensitive).
vendor-b.example
examplevendorb
vendor-b.example
examplevendorb
example vendor a
examplevendora
vendor-a.example
"""


def blocked_senders() -> list[str]:
    raw = get_prompt("blocked_senders", DEFAULT_BLOCKED_SENDERS)
    out = []
    for line in raw.splitlines():
        s = line.strip().lower()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def is_blocked_sender(from_field: str) -> bool:
    """True if the email's From (address and/or display name) matches the blocklist."""
    f = (from_field or "").lower()
    return any(b in f for b in blocked_senders())


def list_prompts() -> list[dict]:
    if not USE_DB:
        return [{"key": k, "content": v[0]} for k, v in _cache.items()]
    rows = db_execute(
        """select key, content, description, updated_at, updated_by
           from agent_prompts order by key""", fetch="all") or []
    return [
        {"key": r[0], "content": r[1], "description": r[2],
         "updated_at": r[3].isoformat() if r[3] else None, "updated_by": r[4]}
        for r in rows
    ]


def get_prompt_row(key: str) -> dict | None:
    if not USE_DB:
        c = _cache.get(key)
        return {"key": key, "content": c[0]} if c else None
    row = db_execute(
        """select key, content, description, updated_at, updated_by
           from agent_prompts where key=%s""", (key,), fetch="one")
    if not row:
        return None
    return {"key": row[0], "content": row[1], "description": row[2],
            "updated_at": row[3].isoformat() if row[3] else None, "updated_by": row[4]}


def set_prompt(key: str, content: str, updated_by: str = "", description: str | None = None) -> bool:
    """Upsert a prompt and bust the local cache so the next read is fresh.
    (Other processes refresh within _TTL seconds.)"""
    if USE_DB:
        if description is None:
            db_execute(
                """insert into agent_prompts (key, content, updated_at, updated_by)
                   values (%s,%s,now(),%s)
                   on conflict (key) do update set
                     content=excluded.content, updated_at=now(),
                     updated_by=excluded.updated_by""",
                (key, content, updated_by))
        else:
            db_execute(
                """insert into agent_prompts (key, content, description, updated_at, updated_by)
                   values (%s,%s,%s,now(),%s)
                   on conflict (key) do update set
                     content=excluded.content, description=excluded.description,
                     updated_at=now(), updated_by=excluded.updated_by""",
                (key, content, description, updated_by))
    _cache.pop(key, None)
    return True
