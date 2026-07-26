"""
Group factoring-company name/address blobs by BRAND, for the proper
brand-company + per-address-location model (NOT a lossy merge).

The sheet records factoring companies as free-text "brand + address" blobs, so the
same brand shows up many ways. We group them under one brand company while keeping
each distinct address as its own company_location with its own banking — so the
per-address banking distinction is preserved.

Used by factoring_restructure.py.
"""
import re

# Brand keyword (matched in the cleaned, lowercased name) → canonical brand name.
# Order matters: more specific first. Only high-confidence brands; anything
# unmatched keeps its own cleaned name (its own brand, one location).
ALIASES = [
    ("apex", "Apex Capital"),
    ("ecapital", "eCapital Freight Factoring"),
    ("frieght funding", "Freight Funding"),
    ("freight funding", "Freight Funding"),
    ("affacturage j d", "JD Factors"),
    ("jd factors", "JD Factors"),
    ("j d factors", "JD Factors"),
    ("riviera", "Riviera Finance"),
    ("rivera", "Riviera Finance"),
    ("truckstop", "Truckstop Factoring"),
    ("triump", "Triumph Business Capital"),
    ("bobtail", "Bobtail"),
    ("bob tail", "Bobtail"),
    ("pro funding", "Pro Funding"),
    ("tru funding", "Tru Funding"),
    ("bvd", "BVD Capital"),
    ("haulpay", "Haulpay"),
    ("haul pay", "Haulpay"),
    ("tafs", "TAFS"),
    ("yankton", "Yankton Factoring"),
    ("blue water", "Blue Water"),
    ("love", "Love's Solutions"),
    ("otr capital", "OTR Capital"),
    ("otr solutions", "OTR Capital"),
    ("compass funding", "Compass Funding Solutions"),
    ("rts financial", "RTS Financial Service"),
    ("rts international", "RTS International"),
    ("invoice payment ststem", "Invoice Payment System Corp"),
    ("invoice payment system", "Invoice Payment System Corp"),
    ("revolution capital", "Revolution Capital"),
    ("stewart", "Stewart Factoring"),
    ("capital depot", "Capital Depot"),
    ("integra funding", "Integra Funding Solutions"),
    ("firstline funding", "Firstline Funding Group"),
    ("parikh financial", "Parikh Financial"),
]

_TRAIL = re.compile(
    r"[,/(]?\s*(p\.?\s*o\.?\s*box|po box|routing|dept|account|bank name|c/o|d/b/a|"
    r"department|deaprtment|station|stn).*$",
    re.IGNORECASE,
)


def _clean(name: str) -> str:
    s = re.split(r"\d", name or "", 1)[0]
    s = s.split("\n")[0]
    s = _TRAIL.sub("", s)
    return s.strip(" ,./\\-(")


def canonical_factoring_name(name: str) -> str:
    """Best-effort canonical BRAND name. Falls back to the cleaned name."""
    base = _clean(name)
    norm = re.sub(r"\s+", " ", base.lower()).strip()
    for kw, canon in ALIASES:
        if kw in norm:
            return canon
    return base or (name or "").strip()
