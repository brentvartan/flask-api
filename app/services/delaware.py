"""
Delaware stealth fundraise signals via SEC EDGAR Form D.

Form D is filed when a company raises its first outside capital under
Regulation D — before any public announcement.  Filtering for Delaware-
incorporated entities gives us stealth consumer brands that are:
  • Committed enough to incorporate in Delaware
  • Actively raising seed/pre-seed money
  • Not yet public

EDGAR's search API is completely free with no API key required.
"""
import re
import time
import logging
import requests

from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
DOMAINSDB_URL    = "https://api.domainsdb.info/v1/domains/search"
REQUEST_TIMEOUT  = 20

# Form D "items" codes that indicate investment funds / VC vehicles — not
# operating companies.  We exclude filings whose only item codes are these.
_FUND_ITEMS = {"06a", "06b", "06c", "3c", "3c.1", "3c.7"}


# ─── Consumer keyword → category map ─────────────────────────────────────────

_CATEGORY_KEYWORDS = {
    "CPG/Food/Drink": [
        "food", "foods", "beverage", "beverages", "drink", "drinks", "bev",
        "brew", "brewery", "brewing", "coffee", "tea", "juice", "bar", "bars",
        "snack", "snacks", "bite", "bites", "eats", "kitchen", "farms", "farm",
        "harvest", "organic", "fresh", "cafe", "bakery", "bake", "wine", "winery",
        "spirits", "distillery", "water", "soda", "nutrition", "nutritional",
        "provisions", "pantry", "table", "grains",
    ],
    "Beauty": [
        "beauty", "cosmetic", "cosmetics", "skincare", "skin", "hair", "haircare",
        "nail", "glow", "radiant", "serum", "spa", "salon", "lash", "brow",
        "bloom", "lush", "glam", "glamour", "fragrance", "scent",
    ],
    "Health/Wellness": [
        "health", "wellness", "vital", "vitality", "well", "heal", "healing",
        "longevity", "clean", "pure", "detox", "supplement", "supplements",
        "vitamin", "vitamins", "probiotic", "remedy", "relief", "therapeutic",
        "mindful", "mindfulness", "balance", "restore",
    ],
    "Apparel": [
        "apparel", "clothing", "wear", "fashion", "style", "dress", "thread",
        "stitch", "cloth", "fabric", "couture", "gear", "denim", "outfitter",
        "outfitters", "wardrobe", "garment",
    ],
    "Fitness": [
        "fitness", "gym", "sport", "sports", "active", "athlete", "athletes",
        "train", "training", "run", "running", "lift", "lifting", "workout",
        "cycle", "cycling", "swim", "movement", "flex", "performance",
    ],
    "Home/Lifestyle": [
        "home", "house", "living", "decor", "design", "interior", "furnish",
        "furniture", "bed", "bath", "clean", "cleaning", "organize", "space",
        "nest", "den", "hearth", "habitat",
    ],
    "Consumer AI": [
        "ai", "intelligence", "intelligent", "smart", "digital", "lab", "labs",
    ],
    "Entertainment": [
        "entertain", "entertainment", "media", "content", "story", "stories",
        "game", "games", "gaming", "music", "art", "film", "studio",
        "creative", "experience",
    ],
    "Education": [
        "learn", "learning", "edu", "education", "school", "tutor", "coaching",
        "skill", "skills", "study", "teach", "academy", "knowledge",
    ],
    "Finance": [
        "fintech", "payment", "payments", "money", "wallet", "credit", "wealth",
    ],
    "Sports": [
        "sport", "sports", "ball", "team", "league", "athlete", "race",
        "compete", "competition",
    ],
    "Beauty": [
        "pet", "pets", "dog", "dogs", "cat", "cats", "paw", "paws",
        "fur", "bark", "vet", "animal", "animals",
    ],
}

_NON_CONSUMER_BLOCKLIST = {
    "holdings", "holding", "capital", "ventures", "venture", "properties",
    "property", "partners", "partnership", "associates", "consulting",
    "consultant", "management", "services", "solutions", "systems",
    "technologies", "technology", "realty", "real estate", "construction",
    "engineering", "logistics", "transport", "law", "legal", "attorneys",
    "attorney", "advisors", "advisory", "group", "fund", "funds",
    "investment", "investments", "asset", "assets", "equity", "securities",
    "acquisition", "acquisitions", "staffing", "recruiting", "insurance",
    "accounting", "leasing", "development",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _strip_legal_suffix(name: str) -> str:
    return re.sub(
        r'\s*,?\s*(LLC|L\.L\.C\.?|Inc\.?|Corp\.?|Corporation|Company|Co\.?'
        r'|Ltd\.?|Limited|L\.P\.?|LLP|PLLC|PC|P\.C\.)\s*$',
        '', name, flags=re.IGNORECASE,
    ).strip()


def _infer_category(name: str) -> str:
    lower = name.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return category
    return "Consumer AI"   # default — AI enrichment will correct this


def _is_consumer_candidate(name: str, items: list) -> bool:
    """Exclude obvious fund/investment vehicles and non-consumer names."""
    lower = name.lower()

    # Reject if ALL item codes indicate an investment fund
    if items:
        non_fund = [i for i in items if i.lower() not in _FUND_ITEMS]
        if not non_fund:
            return False

    if len(lower.split()) > 6:
        return False

    if any(term in lower for term in _NON_CONSUMER_BLOCKLIST):
        return False

    if re.match(r'^[\d\s\-\.]+$', name):
        return False

    return True


def _brand_slug(name: str) -> str:
    """'BRÜ BEVERAGES LLC' → 'bru', 'HEY BRO WINES' → 'heybro'"""
    clean = _strip_legal_suffix(name)
    clean = re.sub(r'[\(\[].*?[\)\]]', '', clean).strip()
    words = clean.split()
    if words and len(words[0]) < 3 and len(words) > 1:
        slug = ''.join(words[:2])
    elif words:
        slug = words[0]
    else:
        slug = clean
    return re.sub(r'[^a-z0-9]', '', slug.lower())


# ─── Domain cross-reference ───────────────────────────────────────────────────

def check_domain(brand_slug: str, days_back: int = 90) -> dict | None:
    """Return domain signal dict if matching .com was recently registered."""
    if not brand_slug or len(brand_slug) < 2:
        return None

    try:
        resp = requests.get(
            DOMAINSDB_URL,
            params={"domain": brand_slug, "zone": "com", "limit": 5},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("DomainsDB lookup failed for %s: %s", brand_slug, exc)
        return None

    cutoff = datetime.utcnow() - timedelta(days=days_back)

    for d in data.get("domains", []):
        domain_name = d.get("domain", "")
        create_raw  = d.get("create_date", "")

        if domain_name.split(".")[0].lower() != brand_slug:
            continue

        try:
            create_dt = datetime.fromisoformat(create_raw.replace("Z", ""))
        except (ValueError, TypeError):
            continue

        if create_dt >= cutoff:
            return {
                "domain":     domain_name,
                "registered": create_raw[:10],
                "url":        f"https://{domain_name}",
            }

    return None


# ─── Main service function ────────────────────────────────────────────────────

def search_recent_delaware_entities(
    days_back: int = 7,
    max_results: int = 200,
    check_domains: bool = True,
) -> dict:
    """
    Fetch recent Form D filings from Delaware-incorporated companies via
    SEC EDGAR (free, no API key required).

    Form D = first outside capital raise, pre-announcement — the earliest
    verifiable signal that a stealth brand is becoming real.

    Returns:
        {
            "signals":      list of signal dicts,
            "total_found":  int,
            "fetched":      int,   # DE entities
            "domain_hits":  int,   # companion domain signals
            "error":        str | None,
        }
    """
    end_dt   = datetime.utcnow()
    start_dt = end_dt - timedelta(days=days_back)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str   = end_dt.strftime("%Y-%m-%d")

    page_size   = 50
    pages_to_fetch = min(4, (max_results // page_size) + 1)

    signals     = []
    total_found = 0
    domain_hits = 0
    seen_names  = set()

    for page in range(pages_to_fetch):
        try:
            resp = requests.get(
                EDGAR_SEARCH_URL,
                params={
                    "q":         "",
                    "forms":     "D",
                    "dateRange": "custom",
                    "startdt":   start_str,
                    "enddt":     end_str,
                    "from":      page * page_size,
                    "size":      page_size,
                },
                headers={
                    "User-Agent": "Bullish Stealth Finder research@bullish.co",
                    "Accept":     "application/json",
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            if page == 0:
                return {
                    "signals": [], "total_found": 0,
                    "fetched": 0, "domain_hits": 0,
                    "error": str(exc),
                }
            break

        hits_obj = data.get("hits", {})
        if page == 0:
            total_found = hits_obj.get("total", {}).get("value", 0)

        raw_hits = hits_obj.get("hits", [])
        if not raw_hits:
            break

        for hit in raw_hits:
            src = hit.get("_source", {})

            # Filter: only Delaware-incorporated companies
            inc_states = [s.upper() for s in (src.get("inc_states") or [])]
            if "DE" not in inc_states:
                continue

            # Extract name (EDGAR returns a list like ["BRAND LLC  (CIK 0001234)"])
            display_names = src.get("display_names") or []
            raw_name = display_names[0] if display_names else ""
            # Strip the "(CIK XXXXXXXX)" suffix EDGAR appends
            name = re.sub(r'\s*\(CIK\s*[\d]+\)\s*$', '', raw_name).strip()

            if not name or name in seen_names:
                continue

            items_list = src.get("items") or []
            if not _is_consumer_candidate(name, items_list):
                continue

            seen_names.add(name)

            brand      = _strip_legal_suffix(name)
            category   = _infer_category(brand)
            file_date  = src.get("file_date", end_dt.strftime("%Y-%m-%d"))
            biz_loc    = (src.get("biz_locations") or [""])[0]
            adsh       = src.get("adsh", "")
            edgar_url  = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{(src.get('ciks') or [''])[0]}/{adsh.replace('-', '')}/{adsh}-index.htm"
                if adsh else "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=D"
            )

            signals.append({
                "companyName": brand,
                "signal_type": "delaware",
                "category":    category,
                "score_boost": 5,
                "description": (
                    f"{name} — Delaware Corp/LLC — Form D filed {file_date}"
                    + (f" — {biz_loc}" if biz_loc else "")
                ),
                "url":         edgar_url,
                "notes":       (
                    f"Pre-raise stealth signal. {name} filed Form D (Reg D exemption) "
                    f"on {file_date}. Incorporated in Delaware."
                    + (f" Operating from {biz_loc}." if biz_loc else "")
                ),
                "timestamp":   file_date + "T00:00:00",
            })

            # Domain cross-reference
            if check_domains and len(signals) <= 60:
                slug = _brand_slug(name)
                if slug and len(slug) >= 3:
                    domain_info = check_domain(slug, days_back=120)
                    if domain_info:
                        domain_hits += 1
                        signals.append({
                            "companyName": brand,
                            "signal_type": "domain",
                            "category":    category,
                            "score_boost": 3,
                            "description": (
                                f"{domain_info['domain']} — registered {domain_info['registered']}"
                                f" — corroborates Form D filing for {name}"
                            ),
                            "url":         domain_info["url"],
                            "notes":       (
                                f"Domain registered {domain_info['registered']}, "
                                f"matching Form D entity {name}."
                            ),
                            "timestamp":   file_date + "T00:00:00",
                        })
                    time.sleep(0.4)

            if len([s for s in signals if s["signal_type"] == "delaware"]) >= max_results:
                break

        if len([s for s in signals if s["signal_type"] == "delaware"]) >= max_results:
            break

        time.sleep(0.5)   # be polite to EDGAR

    de_count = len([s for s in signals if s["signal_type"] == "delaware"])
    return {
        "signals":     signals,
        "total_found": total_found,
        "fetched":     de_count,
        "domain_hits": domain_hits,
        "error":       None,
    }
