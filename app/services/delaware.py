"""
Delaware Division of Corporations — entity filing service.

Queries new LLC/Corp filings via OpenCorporates (free, no API key required
for basic access at ≤10 req/hr).  For each entity found we also check
whether a matching .com domain was recently registered, and if so include
a companion domain signal.
"""
import re
import time
import logging
import requests

from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

OPENCORP_URL  = "https://api.opencorporates.com/v0.4/companies/search"
DOMAINSDB_URL = "https://api.domainsdb.info/v1/domains/search"
REQUEST_TIMEOUT = 15

# ─── Consumer keyword → category map ─────────────────────────────────────────

_CATEGORY_KEYWORDS = {
    "CPG/Food/Drink": [
        "food", "foods", "beverage", "beverages", "drink", "drinks", "bev",
        "brew", "brewery", "brewing", "coffee", "tea", "juice", "bar", "bars",
        "snack", "snacks", "bite", "bites", "eats", "kitchen", "farms", "farm",
        "harvest", "organic", "fresh", "cafe", "bakery", "bake", "wine", "winery",
        "spirits", "distillery", "water", "soda", "nutrition", "nutritional",
        "provisions", "provision", "pantry", "table", "grains", "grain",
    ],
    "Beauty": [
        "beauty", "cosmetic", "cosmetics", "skincare", "skin", "hair", "haircare",
        "nail", "glow", "radiant", "serum", "spa", "salon", "lash", "brow",
        "bloom", "lush", "glam", "glamour", "fragrance", "scent",
    ],
    "Health/Wellness": [
        "health", "wellness", "vital", "vitality", "well", "heal", "healing",
        "cure", "care", "longevity", "clean", "pure", "detox", "supplement",
        "supplements", "vitamin", "vitamins", "probiotic", "remedy", "relief",
        "therapeutic", "mindful", "mindfulness", "balance", "restore",
    ],
    "Apparel": [
        "apparel", "clothing", "wear", "fashion", "style", "styled", "dress",
        "thread", "stitch", "cloth", "fabric", "couture", "gear", "denim",
        "outfitter", "outfitters", "wardrobe", "garment", "garments",
    ],
    "Fitness": [
        "fitness", "gym", "sport", "sports", "active", "athlete", "athletes",
        "train", "training", "run", "running", "lift", "lifting", "workout",
        "cycle", "cycling", "swim", "swimming", "move", "movement", "flex",
        "performance",
    ],
    "Home/Lifestyle": [
        "home", "house", "living", "decor", "design", "interior", "furnish",
        "furniture", "bed", "bath", "clean", "cleaning", "organize", "space",
        "nest", "den", "hearth", "habitat", "dwelling",
    ],
    "Consumer AI": [
        "ai", "intelligence", "intelligent", "smart", "digital", "lab", "labs",
    ],
    "Entertainment": [
        "entertain", "entertainment", "media", "content", "story", "stories",
        "game", "games", "gaming", "play", "music", "art", "film", "studio",
        "creative", "experience", "experiences",
    ],
    "Education": [
        "learn", "learning", "edu", "education", "school", "tutor", "tutoring",
        "coach", "coaching", "skill", "skills", "study", "teach", "academy",
        "knowledge",
    ],
    "Finance": [
        "finance", "fintech", "pay", "payment", "payments", "money", "invest",
        "bank", "credit", "wealth", "debit", "wallet",
    ],
    "Sports": [
        "sport", "sports", "ball", "team", "league", "athlete", "race",
        "compete", "competition", "trophy",
    ],
}

# Names containing any of these terms are almost certainly non-consumer
_NON_CONSUMER_BLOCKLIST = {
    "holdings", "holding", "capital", "ventures", "venture", "properties",
    "property", "partners", "partnership", "associates", "consulting",
    "consultant", "management", "services", "solutions", "systems",
    "technologies", "technology", "realty", "real estate", "construction",
    "engineering", "logistics", "transport", "transportation", "law",
    "legal", "attorneys", "attorney", "advisors", "advisory", "group",
    "fund", "funds", "investment", "investments", "asset", "assets",
    "equity", "securities", "acquisition", "acquisitions", "staffing",
    "staffing", "recruiting", "recruiter", "insurance", "accounting",
    "accounting", "staffing", "leasing", "development", "developer",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _strip_legal_suffix(name: str) -> str:
    """Remove legal entity suffixes from a company name."""
    return re.sub(
        r'\s*,?\s*(LLC|L\.L\.C\.?|Inc\.?|Corp\.?|Corporation|Company|Co\.?'
        r'|Ltd\.?|Limited|L\.P\.?|LLP|PLLC|PC|P\.C\.)\s*$',
        '', name, flags=re.IGNORECASE,
    ).strip()


def _infer_category(name: str) -> str | None:
    """Return a Bullish category from keywords in the brand name, or None."""
    lower = name.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return category
    return None


def _is_consumer_candidate(name: str) -> bool:
    """
    Return True if the entity name could plausibly be a consumer brand.
    Rejects holding-company patterns and very long / generic names.
    """
    lower = name.lower()
    words = lower.split()

    # Must be reasonably short (brand names aren't 8-word sentences)
    if len(words) > 6:
        return False

    # Reject obvious non-consumer terms
    if any(term in lower for term in _NON_CONSUMER_BLOCKLIST):
        return False

    # Reject purely numeric or gibberish names
    if re.match(r'^[\d\s\-\.]+$', name):
        return False

    return True


def _brand_slug(name: str) -> str:
    """
    Extract a URL-friendly slug from a brand name for domain lookup.
    'BRÜ BEVERAGES' → 'bru'
    'HEY BRO WINES' → 'heybro'
    """
    clean = _strip_legal_suffix(name)
    # Drop anything in parens/brackets
    clean = re.sub(r'[\(\[].*?[\)\]]', '', clean).strip()
    words = clean.split()
    # Take first word; if it's very short (<3 chars) take first two
    if words and len(words[0]) < 3 and len(words) > 1:
        slug = ''.join(words[:2])
    elif words:
        slug = words[0]
    else:
        slug = clean
    return re.sub(r'[^a-z0-9]', '', slug.lower())


# ─── Domain cross-reference ───────────────────────────────────────────────────

def check_domain(brand_slug: str, days_back: int = 90) -> dict | None:
    """
    Return domain signal dict if a .com domain matching brand_slug was
    registered within days_back days, otherwise None.
    """
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

        # Only exact match (not subdomains like getbru.com for 'bru')
        if domain_name.split(".")[0].lower() != brand_slug:
            continue

        try:
            create_dt = datetime.fromisoformat(create_raw.replace("Z", ""))
        except (ValueError, TypeError):
            continue

        if create_dt >= cutoff:
            return {
                "domain":      domain_name,
                "registered":  create_raw[:10],
                "url":         f"https://{domain_name}",
            }

    return None


# ─── Main service function ────────────────────────────────────────────────────

def search_recent_delaware_entities(
    days_back: int = 7,
    max_results: int = 200,
    check_domains: bool = True,
) -> dict:
    """
    Fetch new Delaware LLC/Corp filings from OpenCorporates.

    Returns:
        {
            "signals":      [list of signal dicts],
            "total_found":  int,
            "fetched":      int,
            "domain_hits":  int,
            "error":        str | None,
        }
    """
    cutoff = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    per_page = 100
    pages_to_fetch = min(3, (max_results // per_page) + 1)

    signals     = []
    total_found = 0
    domain_hits = 0
    seen_names  = set()   # dedup within this batch

    for page in range(1, pages_to_fetch + 1):
        try:
            resp = requests.get(
                OPENCORP_URL,
                params={
                    "jurisdiction_code": "us_de",
                    "created_since":     cutoff,
                    "per_page":          per_page,
                    "page":              page,
                    "order":             "incorporation_date desc",
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            if page == 1:
                return {
                    "signals": [], "total_found": 0,
                    "fetched": 0, "domain_hits": 0,
                    "error": str(exc),
                }
            break

        results = data.get("results", {})
        if page == 1:
            total_found = results.get("total_count", 0)

        companies = results.get("companies", [])
        if not companies:
            break

        for entry in companies:
            co = entry.get("company", {})

            name    = (co.get("name") or "").strip()
            co_type = (co.get("company_type") or "").strip()
            status  = (co.get("current_status") or "").lower()
            inc_dt  = co.get("incorporation_date") or ""

            # Only active entities
            if status and status not in ("active", "in existence", "good standing", ""):
                continue

            # Only LLC / Corp (not LP, NP, Trust, etc.)
            if co_type and not any(
                t in co_type.lower()
                for t in ("llc", "l.l.c", "corp", "inc", "incorporated", "benefit")
            ):
                continue

            if not name or name in seen_names:
                continue

            if not _is_consumer_candidate(name):
                continue

            seen_names.add(name)
            brand      = _strip_legal_suffix(name)
            category   = _infer_category(brand) or "Consumer AI"
            filed_date = inc_dt or datetime.utcnow().isoformat()
            co_number  = co.get("company_number", "")
            oc_url     = (
                f"https://opencorporates.com/companies/us_de/{co_number}"
                if co_number else "https://icis.corp.delaware.gov/"
            )

            signals.append({
                "companyName": brand,
                "signal_type": "delaware",
                "category":    category,
                "score_boost": 5,
                "description": f"{name} — {co_type or 'LLC'} — Filed {filed_date[:10]}",
                "url":         oc_url,
                "notes":       f"Delaware entity: {name}. Type: {co_type}.",
                "timestamp":   filed_date,
            })

            # Domain cross-reference (rate-limited)
            if check_domains and len(signals) <= 50:
                slug = _brand_slug(name)
                if slug and len(slug) >= 3:
                    domain_info = check_domain(slug, days_back=90)
                    if domain_info:
                        domain_hits += 1
                        signals.append({
                            "companyName": brand,
                            "signal_type": "domain",
                            "category":    category,
                            "score_boost": 3,
                            "description": (
                                f"{domain_info['domain']} — registered {domain_info['registered']}"
                                f" — matches Delaware filing for {name}"
                            ),
                            "url":         domain_info["url"],
                            "notes":       f"Domain corroborates DE filing for {name}.",
                            "timestamp":   filed_date,
                        })
                    time.sleep(0.5)   # be polite to DomainsDB

            if len([s for s in signals if s["signal_type"] == "delaware"]) >= max_results:
                break

        if len([s for s in signals if s["signal_type"] == "delaware"]) >= max_results:
            break

        time.sleep(1)   # be polite to OpenCorporates (10 req/hr free tier)

    return {
        "signals":     signals,
        "total_found": total_found,
        "fetched":     len([s for s in signals if s["signal_type"] == "delaware"]),
        "domain_hits": domain_hits,
        "error":       None,
    }
