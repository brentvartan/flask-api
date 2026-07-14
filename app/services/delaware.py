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
# operating companies.  We exclude filings whose ONLY item codes are these.
# NOTE: "06a"/"06b"/"06c" (Reg D Rule 506 exemptions) must NOT be here —
# every operating company including consumer brands files under 06b.
# Only Investment Company Act fund exemptions belong in this set.
_FUND_ITEMS = {"3c", "3c.1", "3c.5", "3c.7"}


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
    # Entity-type indicators almost never used by consumer brands
    " lp",                               # limited partnership (e.g. "GLF Route 66 LP")
    "l.p.",                              # spelled-out LP form
    "pllc",                              # professional LLC (law, medicine, accounting)
    # SPV / co-invest / syndicate structures (never operating companies)
    "spv",                               # Special Purpose Vehicle
    "co-invest", "co-investing",         # co-investment SPVs
    " series of ",                       # "a Series of CGF2021 LLC" style names
    "gaingels",                          # LGBTQ+ investor SPV operator
    "holdco",                            # holding company abbreviation
    "acretrader",                        # farmland investment platform
    # Real estate / finance non-consumer patterns
    "reit",                              # Real Estate Investment Trust
    "qozb",                              # Qualified Opportunity Zone Business
    "credit union",                      # financial institution
    "bancorp", "bancshares", "bancshar", # banks
    "villas",                            # residential real estate
    " apts", "apartments",               # residential real estate
    "laydown", "storwell", "self storage", "self-storage",
    # Fund structures and offshore vehicles
    "scsp",                              # Luxembourg partnership (fund structure)
    "investco",                          # investment company
    "moonrock",                          # known SPV operator
    # Sector/industry signals almost never consumer brands
    "biosystems",                        # biotech/research
    " metals",                           # mining/extraction
    # Additional non-consumer patterns
    "industrial", "industries",          # manufacturing / industrial
    " dst",                              # Delaware Statutory Trust (real estate)
    "cooperative", " coop",              # farm / housing cooperatives
    "medical", "clinical", "hospital",   # healthcare (not consumer wellness)
    "pharma", "pharmaceutical",
    "biotech", "bioscience",
    "infrastructure",
    "enterprise", "enterprises",
    # NOTE: "development", "services", "solutions", "systems", "group" still
    # excluded — too aggressive; blocks "Brand Development LLC", "Wellness Services LLC".
}

# Regex for numbered investment vehicles: "Fund VIII", "Portfolio XVII LLC", etc.
# Covers Roman numerals II–XLIX (2-49), which is the full range of real-world fund series.
_FUND_NUMERAL_RE = re.compile(
    r'\b(?:'
    r'I{2,3}'                                       # II, III
    r'|IV'                                           # 4
    r'|V(?:I{1,3})?'                                # V, VI, VII, VIII
    r'|IX'                                           # 9
    r'|X{1,3}(?:I{0,3}|IV|V(?:I{0,3})?|IX)?'       # X–XXXIX
    r'|XL(?:I{0,3}|IV|V(?:I{0,3})?)?'              # XL–XLIX
    r')\b',
    re.IGNORECASE
)


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
    return "Home/Lifestyle"  # catch-all for keyword-miss; enrichment will correct


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

    # Numbered investment vehicles (Fund VIII, Portfolio IV, etc.)
    if _FUND_NUMERAL_RE.search(name):
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


def _edgar_query(q: str, start_str: str, end_str: str, from_: int, size: int) -> dict:
    """Run one EDGAR full-text Form D query and return raw JSON."""
    resp = requests.get(
        EDGAR_SEARCH_URL,
        params={
            "q":         q,
            "forms":     "D",
            "dateRange": "custom",
            "startdt":   start_str,
            "enddt":     end_str,
            "from":      from_,
            "size":      size,
        },
        headers={
            "User-Agent": "Bullish Stealth Finder research@bullish.co",
            "Accept":     "application/json",
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _extract_signal(src: dict, seen_names: set, end_dt: datetime) -> dict | None:
    """Parse one EDGAR hit and return a signal dict, or None if filtered."""
    inc_states = [s.upper() for s in (src.get("inc_states") or [])]
    inc_state  = inc_states[0] if inc_states else "US"

    display_names = src.get("display_names") or []
    raw_name = display_names[0] if display_names else ""
    name = re.sub(r'\s*\(CIK\s*[\d]+\)\s*$', '', raw_name).strip()

    if not name or name in seen_names:
        return None

    items_list = src.get("items") or []
    if not _is_consumer_candidate(name, items_list):
        return None

    brand     = _strip_legal_suffix(name)
    category  = _infer_category(brand)
    file_date = src.get("file_date", end_dt.strftime("%Y-%m-%d"))
    biz_loc   = (src.get("biz_locations") or [""])[0]
    adsh      = src.get("adsh", "")
    _cik      = (src.get("ciks") or [""])[0]
    edgar_url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{_cik}/{adsh.replace('-', '')}/{adsh}-index.htm"
        if adsh else "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=D"
    )

    # BVI and Luxembourg are offshore fund/crypto jurisdictions at this stage;
    # legitimate consumer brand operating companies don't incorporate there at seed.
    if inc_state in {"D8", "N4"}:
        return None

    return {
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
        "_adsh":       adsh,
        "_cik":        _cik,
        "_name":       name,  # original full legal name for domain slug
    }


def search_recent_delaware_entities(
    days_back: int = 7,
    max_results: int = 200,
    check_domains: bool = True,
    max_filings: int = 6000,
) -> dict:
    """
    Fetch recent Form D filings from consumer companies via SEC EDGAR (free,
    no API key required) — all 50 US states.

    Strategy:
    BROAD SWEEP — probe for the true Form D count in the window, then page
    through the FULL universe (up to max_filings). At ~0.25% consumer rate,
    a 7-day window (~1,400 filings) yields ~3–4 consumer brands per run.
    Targeted keyword queries were tried and removed: EDGAR full-text search
    matches officer names and addresses, not just company names, so terms
    like "food" or "beauty" return near-zero results and add no signal.
    Invented-name brands (Fascent, Rivalz, Mosh) are only findable by
    sweeping the full universe with _is_consumer_candidate filtering.

    DOMAIN CHECK — for each consumer brand found, cross-reference .com
    domain registration (corroborates stealth; adds a second signal).

    Returns:
        {
            "signals":      list of signal dicts,
            "total_found":  int,
            "fetched":      int (Form D signals only),
            "domain_hits":  int,
            "error":        str | None,
        }
    """
    end_dt    = datetime.utcnow()
    start_dt  = end_dt - timedelta(days=days_back)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str   = end_dt.strftime("%Y-%m-%d")

    signals     = []
    total_found = 0
    domain_hits = 0
    seen_names  = set()
    page_size   = 100
    error       = None

    # ── Probe: discover true Form D count in this window ──────────────────────
    try:
        probe       = _edgar_query("", start_str, end_str, 0, 1)
        total_found = probe.get("hits", {}).get("total", {}).get("value", 0)
    except requests.RequestException as exc:
        return {"signals": [], "total_found": 0,
                "fetched": 0, "domain_hits": 0, "error": str(exc)}

    # ── Broad sweep: cover the full universe up to max_filings ────────────────
    pages_needed = (total_found + page_size - 1) // page_size
    pages_cap    = (max_filings + page_size - 1) // page_size
    pages_broad  = min(pages_needed, pages_cap)

    for page in range(pages_broad):
        if len(signals) >= max_results:
            break
        try:
            data = _edgar_query("", start_str, end_str, page * page_size, page_size)
        except requests.RequestException as exc:
            error = str(exc)
            break

        raw_hits = data.get("hits", {}).get("hits", [])
        if not raw_hits:
            break

        for hit in raw_hits:
            if len(signals) >= max_results:
                break
            sig = _extract_signal(hit.get("_source", {}), seen_names, end_dt)
            if sig:
                seen_names.add(sig["_name"])
                signals.append(sig)

        time.sleep(0.5)

    # ── Domain cross-reference ─────────────────────────────────────────────────
    # signals is all Form D at this point; iterate a snapshot before appending domains
    formd_signals = list(signals)
    if check_domains:
        for sig in formd_signals[:60]:
            slug = _brand_slug(sig.get("_name", sig["companyName"]))
            if not slug or len(slug) < 3:
                continue
            domain_info = check_domain(slug, days_back=120)
            if domain_info:
                domain_hits += 1
                signals.append({
                    "companyName": sig["companyName"],
                    "signal_type": "domain",
                    "category":    sig["category"],
                    "score_boost": 3,
                    "description": (
                        f"{domain_info['domain']} — registered {domain_info['registered']}"
                        f" — corroborates Form D filing for {sig.get('_name', sig['companyName'])}"
                    ),
                    "url":         domain_info["url"],
                    "notes":       (
                        f"Domain registered {domain_info['registered']}, "
                        f"matching Form D entity {sig.get('_name', sig['companyName'])}."
                    ),
                    "timestamp":   sig["timestamp"],
                })
            time.sleep(0.4)

    # Strip internal-only keys before returning
    for s in signals:
        s.pop("_name", None)

    return {
        "signals":     signals,
        "total_found": total_found,
        "fetched":     len(formd_signals),
        "domain_hits": domain_hits,
        "error":       error,
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
