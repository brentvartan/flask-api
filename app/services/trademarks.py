"""
USPTO Trademark Center service.

Queries the undocumented Elasticsearch-backed API used by
https://tmsearch.uspto.gov to fetch recent consumer trademark filings.
No API key required — this is the same endpoint the public search UI uses.
"""
import re
import requests
from datetime import datetime, timedelta

USPTO_SEARCH_URL = "https://tmsearch.uspto.gov/prod-stage-v1-0-0/tmsearch"
REQUEST_TIMEOUT = 20  # seconds

# ── Consumer IC class → category mapping ──────────────────────────────────────
IC_CATEGORY_MAP = {
    "IC 003": "Beauty",           # Cosmetics, skincare, haircare, toiletries
    "IC 005": "Health/Wellness",  # Supplements, pharma, nutraceuticals, medical
    "IC 025": "Apparel",          # Clothing, footwear, headwear
    "IC 028": "Sports",           # Toys, games, sporting goods
    "IC 029": "CPG/Food/Drink",   # Meat, dairy, preserved / processed foods
    "IC 030": "CPG/Food/Drink",   # Coffee, tea, bakery, confectionery
    "IC 031": "CPG/Food/Drink",   # Fresh fruit, vegetables, live animals
    "IC 032": "CPG/Food/Drink",   # Beer, soft drinks, mineral water
    "IC 033": "CPG/Food/Drink",   # Wine, spirits, liqueurs
    "IC 035": "Home/Lifestyle",   # Retail / e-commerce services
    "IC 041": "Education",        # Education, entertainment, fitness training
    "IC 044": "Health/Wellness",  # Medical, beauty, spa, veterinary
}

CONSUMER_CLASSES = list(IC_CATEGORY_MAP.keys())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _infer_category(ic_classes: list) -> str:
    """Return the best consumer category for a list of IC class strings."""
    for ic in ic_classes:
        if ic in IC_CATEGORY_MAP:
            return IC_CATEGORY_MAP[ic]
    return "Other"


def _clean_owner(raw: str) -> str:
    """
    Strip the entity-type / jurisdiction annotation USPTO appends.
    'Acme Labs LLC (LIMITED LIABILITY COMPANY; Delaware, USA)' → 'Acme Labs LLC'
    """
    if not raw:
        return "Unknown"
    cleaned = re.sub(r"\s*\([^)]+\)\s*$", "", raw).strip()
    return cleaned or raw


def _gs_snippet(goods_services: list) -> str:
    """Return a short human-readable snippet of the goods/services."""
    if not goods_services:
        return ""
    first = goods_services[0]
    # Remove the "IC XXX: " prefix and truncate at first semicolon
    first = re.sub(r"^IC \d{3}:\s*", "", first)
    return first.split(";")[0].strip()[:100]


def _fmt_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT00:00:00")


# ── Main service function ─────────────────────────────────────────────────────

def search_recent_trademarks(days_back: int = 30, max_results: int = 200) -> dict:
    """
    Query USPTO for consumer trademark filings in the last *days_back* days.

    Returns:
        {
            "signals": [...],       # list of signal dicts ready to be stored
            "total_found": int,     # total matching filings in the index
            "fetched": int,         # number of signals in this response
            "error": str | None,    # set only on failure
        }
    """
    end_dt = datetime.utcnow()
    start_dt = end_dt - timedelta(days=days_back)

    es_query = {
        "query": {
            "bool": {
                "must": [
                    {
                        "range": {
                            "filedDate": {
                                "gte": _fmt_date(start_dt),
                                "lte": _fmt_date(end_dt),
                            }
                        }
                    }
                ],
                "filter": [
                    {"term": {"alive": True}}
                ],
                # At least one consumer IC class must appear in goodsAndServices
                "should": [
                    {"match_phrase": {"goodsAndServices": ic}}
                    for ic in CONSUMER_CLASSES
                ],
                "minimum_should_match": 1,
            }
        },
        "size": max_results,
        "from": 0,
        "track_total_hits": True,
        "_source": [
            "filedDate",
            "wordmark",
            "ownerName",
            "goodsAndServices",
            "internationalClass",
            "registrationId",
        ],
    }

    try:
        resp = requests.post(
            USPTO_SEARCH_URL,
            json=es_query,
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        return {"signals": [], "total_found": 0, "fetched": 0, "error": str(exc)}

    hits_obj = data.get("hits", {})
    total_found = hits_obj.get("totalValue", 0)
    raw_hits = hits_obj.get("hits", [])

    signals = []
    for hit in raw_hits:
        src = hit.get("source", {})
        wordmark = src.get("wordmark")

        # Skip design-only marks (no text wordmark)
        if not wordmark:
            continue

        ic_classes = src.get("internationalClass", [])
        category = _infer_category(ic_classes)

        # Skip any non-consumer trademarks that slipped through the filter
        if category == "Other":
            continue

        owners = src.get("ownerName", [])
        owner = _clean_owner(owners[0]) if owners else "Unknown"

        filed_date = src.get("filedDate", "")
        filed_label = filed_date[:10] if filed_date else "unknown date"

        # Primary class for display
        primary_class = next(
            (ic for ic in ic_classes if ic in IC_CATEGORY_MAP),
            ic_classes[0] if ic_classes else ""
        )

        gs_list = src.get("goodsAndServices", [])
        snippet = _gs_snippet(gs_list)

        search_url = (
            f"https://tmsearch.uspto.gov/search/search-results"
            f"?searchInput={requests.utils.quote(wordmark)}&dateOption=custom"
        )

        signals.append({
            "companyName":  wordmark,
            "signal_type":  "trademark",
            "category":     category,
            "score_boost":  15,
            "description":  f"{wordmark} — {primary_class} — Filed {filed_label}",
            "url":          search_url,
            "notes":        f"Owner: {owner}. {snippet}".strip(". "),
            "timestamp":    filed_date or end_dt.isoformat(),
        })

    return {
        "signals":     signals,
        "total_found": total_found,
        "fetched":     len(signals),
        "error":       None,
    }
