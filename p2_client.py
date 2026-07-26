"""
Push extracted invoices to project 2 (the ERP dashboard) over its REST API.

This is the "engine → project 2" feed: after the agent verifies an invoice, it
POSTs the result to project 2's service-key-authed ingest endpoint, which upserts
the shipper/carrier invoice onto the matching load (idempotently). Project 2 owns
its own validation + audit log, so data integrity stays in one place.

Entirely opt-in: if P2_API_URL / P2_SERVICE_KEY aren't set, every call is a
no-op (returns None) and the agent behaves exactly as before. No behaviour
toggle — just "active when configured."
"""
import os

import requests
from loguru import logger

P2_API_URL = os.environ.get("P2_API_URL", "").rstrip("/")
P2_SERVICE_KEY = os.environ.get("P2_SERVICE_KEY", "")


def feed_enabled() -> bool:
    return bool(P2_API_URL and P2_SERVICE_KEY)


def _post(payload: dict, timeout: int = 15) -> dict | None:
    if not feed_enabled():
        return None
    try:
        r = requests.post(
            f"{P2_API_URL}/api/ingest/invoice",
            json=payload,
            headers={"X-Service-Key": P2_SERVICE_KEY, "Content-Type": "application/json"},
            timeout=timeout,
        )
        if r.status_code == 200:
            return r.json()
        logger.warning("project 2 feed {}: {}", r.status_code, r.text[:200])
    except Exception as exc:  # never let the feed break a batch run
        logger.warning("project 2 feed failed: {}", exc)
    return None


def push_shipper_invoice(load_number, invoice_number, invoice_date, amount,
                         tax_amount=0, currency=None) -> dict | None:
    """Upsert a shipper (AR) invoice onto an existing load in project 2."""
    return _post({
        "type": "shipper",
        "load_number": str(load_number),
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,   # 'YYYY-MM-DD'
        "amount": amount,
        "tax_amount": tax_amount or 0,
        "currency": currency,
    })


def push_carrier_invoice(load_number, invoice_number, received_date, amount,
                         factoring=False, factoring_company_name=None,
                         currency=None) -> dict | None:
    """Upsert a carrier (AP) invoice onto an existing load in project 2."""
    return _post({
        "type": "carrier",
        "load_number": str(load_number),
        "invoice_number": invoice_number,
        "received_date": received_date,  # 'YYYY-MM-DD'
        "amount": amount,
        "factoring": bool(factoring),
        "factoring_company_name": factoring_company_name,
        "currency": currency,
    })
