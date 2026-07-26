"""One-off: seed the local JSON agent-state files into Postgres.

Run once, locally, AFTER setting DATABASE_URL (so both the JSON files and the DB
are reachable). Idempotent — uses upserts, so re-running is safe. This preserves
the AI extraction cache (so the backend doesn't re-burn the API) and the
processed / forward-pending state when moving off the ephemeral JSON files.

    DATABASE_URL=postgresql://... python migrate_state_to_db.py
"""
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

if not os.environ.get("DATABASE_URL", "").strip():
    sys.exit("DATABASE_URL is not set — set it (Supabase pooler string) and re-run.")

import state  # noqa: E402  — imports after env is loaded so USE_DB is True

if not state.USE_DB:
    sys.exit("state.USE_DB is False — DATABASE_URL not picked up.")

HERE = os.path.dirname(__file__)


def _load(name: str) -> dict:
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def _split_key(key: str) -> tuple[str, str]:
    msg, _, att = key.partition("::")
    return msg, att


def main() -> None:
    # Extraction cache
    cache = _load("extraction_cache.json")
    for key, entry in cache.items():
        msg, att = _split_key(key)
        data = entry.get("data") if isinstance(entry, dict) else None
        if data:
            state.cache_extraction(msg, att, data)
    print(f"  extractions:      seeded {len(cache)}")

    # Processed invoices
    processed = _load("processed_invoices.json")
    for key, v in processed.items():
        msg, att = _split_key(key)
        state.mark_processed(
            msg, att,
            invoice_number=v.get("invoice_number", "") if isinstance(v, dict) else "",
            carrier_name=v.get("carrier_name", "") if isinstance(v, dict) else "",
            sheet_row=v.get("sheet_row") if isinstance(v, dict) else None,
        )
    print(f"  processed:        seeded {len(processed)}")

    # Forward-pending queue
    fwd = _load("forward_pending.json")
    for msg, v in fwd.items():
        if isinstance(v, dict):
            state.add_forward_pending(
                msg, v.get("load_no", ""), v.get("subject", ""),
                v.get("from_addr", ""), v.get("recv_date", ""))
    print(f"  forward_pending:  seeded {len(fwd)}")

    # Per-load forward guard (dispatch-dedupe)
    fwd_loads = _load("forwarded_loads.json")
    for load_no, v in fwd_loads.items():
        state.mark_load_forwarded(
            load_no, v.get("message_id", "") if isinstance(v, dict) else "")
    print(f"  forwarded_loads:  seeded {len(fwd_loads)}")

    print("Done. Verify with: select count(*) from agent_extractions;")


if __name__ == "__main__":
    main()
