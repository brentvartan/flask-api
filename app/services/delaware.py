"""
Stealth fundraise signals via SEC EDGAR Form D — all 50 states.

Form D is filed when a company raises its first outside capital under
Regulation D — before any public announcement.  This covers every US state
because Form D is a federal SEC filing, not a state filing.  Catching all
states means we see scrappy founders who incorporated in Texas, Florida,
California, etc. before converting to Delaware for a VC round.

Stealth consumer brands caught here are:
  • Taking their first friends-and-family or angel money
  • Pre-announcement — not yet on Crunchbase or AngelList
  • Building before any VC has seen the deck

EDGAR's search API is completely free with no API key required.
"""
import json
import re
import time
import logging
import xml.etree.ElementTree as ET
import requests

from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Deferred imports to avoid circular dependency at module load time
# (extensions.db and models.item.Item are only needed by check_domains_in_background)
def _get_db_and_item():
    from ..extensions import db
    from ..models.item import Item
    return db, Item

EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
DOMAINSDB_URL    = "https://api.domainsdb.info/v1/domains/search"
REQUEST_TIMEOUT  = 20

# Form D "items" codes that indicate investment funds / VC vehicles — not
# operating companies.  We exclude filings whose only item codes are these.
_FUND_ITEMS = {"06a", "06b", "06c", "3c", "3c.1", "3c.7"}


# ─── Consumer keyword → category map ─────────────────────────────────────────

from ..utils.categories import CATEGORY_KEYWORDS as _CATEGORY_KEYWORDS

_NON_CONSUMER_BLOCKLIST = {
    "holdings", "holding", "capital", "ventures", "venture", "properties",
    "property", "partners", "partnership", "associates", "consulting",
    "consultant", "management", "technologies", "technology", "realty",
    "real estate", "construction", "engineering", "logistics", "transport",
    "law", "legal", "attorneys", "attorney", "advisors", "advisory",
    "fund", "funds", "investment", "investments", "asset", "assets",
    "equity", "securities", "acquisition", "acquisitions", "staffing",
    "recruiting", "accounting", "leasing",
    # NOTE: "development", "services", "solutions", "systems", "group" intentionally
    # excluded — too aggressive; blocks "Brand Development LLC", "Wellness Services LLC", etc.
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

def fetch_form_d_related_persons(adsh: str, cik: str) -> list[dict]:
    """
    Fetch the EDGAR Form D XML filing and extract Related Persons.

    Form D XML stores every director, officer, and significant owner who was
    named in the filing — i.e. the #1–4 people who just made someone a legal
    promise.  This is the earliest structured record of who is building the brand.

    Args:
        adsh: EDGAR accession number e.g. "0001234567-24-000123"
        cik:  EDGAR CIK number (digits only)

    Returns:
        List of {"name": str, "relationships": list[str]}, empty list on failure.
    """
    if not adsh or not cik:
        return []

    adsh_clean = adsh.replace("-", "")
    url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik}/{adsh_clean}/{adsh}.xml"
    )
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Bullish Stealth Finder research@bullish.co"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.debug("Form D XML fetch HTTP %d for %s", resp.status_code, adsh)
            return []

        root = ET.fromstring(resp.content)
        persons = []

        # EDGAR Form D XML uses tags like {http://...}relatedPersonInfo or plain tags
        # depending on version — iterate all descendants and match by local name.
        for elem in root.iter():
            local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if local != "relatedPersonInfo":
                continue

            first = last = ""
            relationships: list[str] = []

            for child in elem.iter():
                child_local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if child_local == "firstName" and child.text:
                    first = child.text.strip()
                elif child_local == "lastName" and child.text:
                    last = child.text.strip()
                elif child_local == "relationship" and child.text:
                    relationships.append(child.text.strip())

            full_name = f"{first} {last}".strip()
            if full_name:
                persons.append({"name": full_name, "relationships": relationships})

        logger.debug("Form D %s: found %d related persons", adsh, len(persons))
        return persons

    except ET.ParseError as exc:
        logger.debug("Form D XML parse error for %s: %s", adsh, exc)
        return []
    except Exception as exc:
        logger.debug("Form D related persons fetch failed for %s: %s", adsh, exc)
        return []


def search_recent_delaware_entities(
    days_back: int = 7,
    max_results: int = 200,
    check_domains: bool = True,
) -> dict:
    """
    Fetch recent Form D filings from consumer-keyword-matched companies via
    SEC EDGAR (free, no API key required) — all 50 US states.

    Form D = first outside capital raise (friends/family, angels, pre-seed) —
    filed within 15 days of first securities sale, before any public announcement.
    This is the earliest verifiable signal a stealth brand is becoming real.

    Previously filtered to Delaware-incorporated entities only; now catches all
    states so we see founders who haven't yet converted to Delaware for VC.

    Returns:
        {
            "signals":      list of signal dicts,
            "total_found":  int,
            "fetched":      int,   # consumer brand Form D filings found
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

            # Accept all US states — Form D is a federal filing, not state-specific.
            # Delaware incorporation is a proxy for VC-track intent but many early
            # founders incorporate in their home state first and convert later.
            inc_states = [s.upper() for s in (src.get("inc_states") or [])]
            inc_state  = inc_states[0] if inc_states else "US"

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

            _cik = (src.get("ciks") or [""])[0]
            signals.append({
                "companyName": brand,
                "signal_type": "delaware",
                "category":    category,
                "score_boost": 5,
                "description": (
                    f"{name} — {inc_state} Corp/LLC — Form D filed {file_date}"
                    + (f" — {biz_loc}" if biz_loc else "")
                ),
                "url":         edgar_url,
                "notes":       (
                    f"Angel/pre-seed signal. {name} filed Form D (Reg D exemption) "
                    f"on {file_date}. Incorporated in {inc_state}."
                    + (f" Operating from {biz_loc}." if biz_loc else "")
                    + (" (Not yet Delaware — likely pre-VC stage.)" if inc_state != "DE" else "")
                ),
                "timestamp":   file_date + "T00:00:00",
                # Stored for related-persons lookup in routes.py — not displayed
                "_adsh":       adsh,
                "_cik":        _cik,
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

    formd_count = len([s for s in signals if s["signal_type"] == "delaware"])
    return {
        "signals":     signals,
        "total_found": total_found,
        "fetched":     formd_count,
        "domain_hits": domain_hits,
        "error":       None,
    }


def check_domains_in_background(app, user_id: int, form_d_items: list, enrich_fn):
    """
    Background thread: domain cross-reference for a batch of Form D signals.
    enrich_fn: callable(app, item_ids) — passed from routes to avoid circular import.

    form_d_items is a list of (item_id, brand_name, category, timestamp) tuples.
    Each call hits DomainsDB with a 0.4s sleep between requests so we don't hammer
    the API. Domain hits are saved as new 'domain' signal items and queued for
    enrichment. This keeps the /delaware route fast (no inline domain checks).
    """
    import hashlib as _hashlib
    import re as _re
    import time as _time

    db, Item = _get_db_and_item()

    with app.app_context():
        for item_id, brand_name, category, timestamp in form_d_items:
            try:
                slug = _brand_slug(brand_name)
                if not slug or len(slug) < 3:
                    continue

                domain_info = check_domain(slug, days_back=120)
                if not domain_info:
                    _time.sleep(0.4)
                    continue

                # Inline fingerprint (avoids importing from routes)
                _norm = _re.sub(r'\s+', ' ', brand_name.upper().strip())
                fp = _hashlib.sha256(f"domain:{_norm}:{timestamp[:10]}".encode()).hexdigest()[:16]

                already_exists = (
                    Item.query
                    .filter_by(owner_id=user_id)
                    .filter(Item.description.contains(f'"fp":"{fp}"'))
                    .first()
                )
                if already_exists:
                    _time.sleep(0.4)
                    continue

                domain_item = Item(
                    title=brand_name,
                    owner_id=user_id,
                    item_type="signal",
                    description=json.dumps({
                        "_type":        "signal",
                        "fp":           fp,
                        "company_name": brand_name,
                        "signal_type":  "domain",
                        "category":     category,
                        "score_boost":  3,
                        "description":  (
                            f"{domain_info['domain']} — registered {domain_info['registered']}"
                            f" — corroborates Form D filing for {brand_name}"
                        ),
                        "url":          domain_info["url"],
                        "notes":        (
                            f"Domain registered {domain_info['registered']}, "
                            f"matching Form D entity {brand_name}."
                        ),
                        "timestamp":    timestamp,
                    }, separators=(",", ":")),
                )
                db.session.add(domain_item)
                db.session.commit()
                logger.info(
                    "Domain hit (bg): %s → %s saved as item %d",
                    brand_name, domain_info["domain"], domain_item.id,
                )
                enrich_fn(app, [domain_item.id])

            except Exception as exc:
                logger.debug("BG domain check failed for %s: %s", brand_name, exc)
            finally:
                _time.sleep(0.4)
