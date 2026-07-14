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
    "IC 009": "Consumer AI",      # Electronics, wearables, consumer tech, software
    "IC 014": "Apparel",          # Jewelry, watches, precious metals
    "IC 016": "Home/Lifestyle",   # Paper goods, stationery, notebooks, prints
    "IC 018": "Apparel",          # Leather goods, bags, handbags, backpacks
    "IC 020": "Home/Lifestyle",   # Furniture, mirrors, picture frames
    "IC 021": "Home/Lifestyle",   # Kitchen tools, cookware, housewares, glassware
    "IC 024": "Home/Lifestyle",   # Textiles, fabric goods, bed/bath linens
    "IC 025": "Apparel",          # Clothing, footwear, headwear
    "IC 028": "Sports",           # Toys, games, sporting goods
    "IC 029": "CPG/Food/Drink",   # Meat, dairy, preserved / processed foods
    "IC 030": "CPG/Food/Drink",   # Coffee, tea, bakery, confectionery
    "IC 031": "CPG/Food/Drink",   # Fresh fruit, vegetables, live animals
    "IC 032": "CPG/Food/Drink",   # Beer, soft drinks, mineral water
    "IC 033": "CPG/Food/Drink",   # Wine, spirits, liqueurs
    "IC 035": "Home/Lifestyle",   # Retail / e-commerce services
    "IC 041": "Education",        # Education, entertainment, fitness training
    "IC 043": "CPG/Food/Drink",   # Food/beverage services, restaurants, cafes
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


# Owner-name terms that mark an institutional / non-brand filer. The consumer-IC
# query still returns tons of trademarks owned by law firms, holding companies,
# banks, universities, etc. We reject those BEFORE enrichment so the bounded
# per-run signal budget is spent on plausible venture-track brands. Do NOT list
# LLC/Inc/Corp/Co — those are exactly how a new brand entity files.
_NON_BRAND_OWNER = {
    "holdings", "holding", "capital", "ventures", "venture", "partners",
    "l.p.", " lp", "trust", "properties", "property", "realty", "real estate",
    "bank", "bancorp", "insurance", "university", "college", "hospital",
    "health system", "church", "ministries", "diocese", "law", "attorneys",
    "attorney", "llp", "pllc", "consulting", "advisors", "advisory",
    "management", "staffing", "logistics", "associates", "foundation",
}


def _is_brand_candidate(owner: str, wordmark: str) -> bool:
    """Reject obvious institutional / non-brand trademark owners."""
    if not wordmark or not wordmark.strip():
        return False
    o = f" {(owner or '').lower()} "
    # Strip entity suffixes so 'Capital' in 'Capital Foods LLC' still counts but
    # the LLC/Inc themselves never trigger a reject.
    o = re.sub(r"\b(llc|inc|corp|co|ltd|company|corporation)\b\.?", " ", o)
    return not any(term in o for term in _NON_BRAND_OWNER)


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

def _es_query(start_dt: datetime, end_dt: datetime, size: int, frm: int) -> dict:
    """Build the consumer-class Form/trademark ES query for one page.

    Sorted by filedDate DESC so paging is deterministic and returns the FRESHEST
    filings first (the pre-2026-07 query had no sort → the capped page was
    relevance-ranked, not recent).
    """
    return {
        "query": {
            "bool": {
                "must": [
                    {"range": {"filedDate": {"gte": _fmt_date(start_dt),
                                             "lte": _fmt_date(end_dt)}}}
                ],
                "filter": [{"term": {"alive": True}}],
                # At least one consumer IC class must appear in goodsAndServices
                "should": [
                    {"match_phrase": {"goodsAndServices": ic}}
                    for ic in CONSUMER_CLASSES
                ],
                "minimum_should_match": 1,
            }
        },
        "sort": [{"filedDate": {"order": "desc"}}],
        "size": size,
        "from": frm,
        "track_total_hits": True,
        "_source": [
            "filedDate", "wordmark", "ownerName",
            "goodsAndServices", "internationalClass", "registrationId",
            "basisFiled",  # 1b = intent-to-use (pre-launch), 1a = already in commerce
        ],
    }


def search_recent_trademarks(
    days_back: int = 30,
    max_results: int = 250,
    max_filings: int = 1200,
) -> dict:
    """
    Query USPTO for consumer trademark filings in the last *days_back* days.

    Trademark is the app's longest-lead signal (6-28 months pre-launch), but a
    30-day window holds ~39,000 consumer-IC-class filings. We cannot enrich them
    all, so the pre-2026-07 approach (grab first 200, no sort) sampled ~0.5% and
    not even the fresh ones. This version instead makes those bounded slots COUNT:
      • sort by filedDate DESC — the newest filings, deterministically paged;
      • page up to `max_filings` so the owner filter has depth to work with;
      • reject institutional/non-brand owners (_is_brand_candidate) BEFORE the
        `max_results` enrichment budget is spent.
    Net: same order-of-magnitude cost, far higher signal quality. `max_results`
    is the enrich budget; `max_filings` is how deep we page to fill it.

    Returns:
        {"signals": [...], "total_found": int, "fetched": int, "error": str|None,
         "inspected": int}   # inspected = raw filings paged through
    """
    end_dt = datetime.utcnow()
    start_dt = end_dt - timedelta(days=days_back)

    page_size = 100
    pages = max(1, min(20, -(-max_filings // page_size)))
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://tmsearch.uspto.gov",
        "Referer": "https://tmsearch.uspto.gov/search/search-results",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    signals = []
    total_found = 0
    inspected = 0
    seen = set()
    error = None

    for page in range(pages):
        try:
            resp = requests.post(
                USPTO_SEARCH_URL,
                json=_es_query(start_dt, end_dt, page_size, page * page_size),
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            if page == 0:
                return {"signals": [], "total_found": 0, "fetched": 0,
                        "inspected": 0, "error": str(exc)}
            error = str(exc)
            break

        hits_obj = data.get("hits", {})
        if page == 0:
            total_found = hits_obj.get("totalValue", 0)
        raw_hits = hits_obj.get("hits", [])
        if not raw_hits:
            break

        for hit in raw_hits:
            inspected += 1
            src = hit.get("source", {})
            wordmark = src.get("wordmark")

            # Skip design-only marks (no text wordmark)
            if not wordmark:
                continue

            key = wordmark.strip().lower()
            if key in seen:
                continue

            ic_classes = src.get("internationalClass", [])
            category = _infer_category(ic_classes)
            # Skip any non-consumer trademarks that slipped through the filter
            if category == "Other":
                continue

            owners = src.get("ownerName", [])
            owner = _clean_owner(owners[0]) if owners else "Unknown"
            # Reject institutional / non-brand owners before spending enrich budget
            if not _is_brand_candidate(owner, wordmark):
                continue

            seen.add(key)

            filed_date = src.get("filedDate", "")
            filed_label = filed_date[:10] if filed_date else "unknown date"
            primary_class = next(
                (ic for ic in ic_classes if ic in IC_CATEGORY_MAP),
                ic_classes[0] if ic_classes else ""
            )
            snippet = _gs_snippet(src.get("goodsAndServices", []))
            search_url = (
                f"https://tmsearch.uspto.gov/search/search-results"
                f"?searchInput={requests.utils.quote(wordmark)}&dateOption=custom"
            )

            # basisFiled: "1b" = intent-to-use (pre-launch), "1a" = already in commerce
            basis_raw = src.get("basisFiled") or []
            if isinstance(basis_raw, str):
                basis_raw = [basis_raw]
            is_itu = any(b.lower().replace("(", "").replace(")", "") in ("1b", "1 b") for b in basis_raw)
            score_boost = 18 if is_itu else 14
            basis_note = " · pre-launch intent-to-use" if is_itu else ""
            desc = f"{wordmark} — {primary_class} — Filed {filed_label}{basis_note}"

            signals.append({
                "companyName":       wordmark,
                "signal_type":       "trademark",
                "category":          category,
                "score_boost":       score_boost,
                "is_intent_to_use":  is_itu,
                "description":       desc,
                "url":               search_url,
                "notes":             f"Owner: {owner}. {snippet}".strip(". "),
                "timestamp":         filed_date or end_dt.isoformat(),
            })

            if len(signals) >= max_results:
                break

        if len(signals) >= max_results:
            break
        if (page + 1) * page_size >= total_found:
            break

    return {
        "signals":     signals,
        "total_found": total_found,
        "fetched":     len(signals),
        "inspected":   inspected,
        "error":       error,
    }
