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
import re
import time
import logging
import threading
import xml.etree.ElementTree as ET
import requests

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import lru_cache

logger = logging.getLogger(__name__)

EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
REQUEST_TIMEOUT  = 20

# ─── EDGAR resilience knobs ──────────────────────────────────────────────────
# efts.sec.gov intermittently returns 500s during SEC's overnight maintenance
# window (the same URL is healthy at midday).  With zero retry, one transient
# blip killed an entire night's Form D collection, so every full-text query
# retries with exponential backoff before giving up.
EDGAR_MAX_ATTEMPTS    = 4                        # 1 initial try + 3 retries
EDGAR_BACKOFF_BASE    = 2                        # sleeps of 2s, 4s, 8s
EDGAR_MAX_BACKOFF     = 60                       # ceiling for a Retry-After header
EDGAR_RETRY_STATUSES  = {429, 500, 502, 503, 504}

# When the count probe itself fails, sweep this many filings blind rather than
# returning nothing — a probe outage must degrade coverage, never zero it.
EDGAR_PROBE_FALLBACK_FILINGS = 1500

# ─── Form D officer extraction: coverage + throttle knobs ────────────────────
# Officers named on a Form D are the most valuable field this product collects:
# the people who just made a legal promise to an investor, matched against the
# 193-person conviction/alumni watchlist.  The old serial loop covered only the
# first 50 signals of a run, so the large majority of every week got NO officer
# extraction at all — no related_persons, no filer_name, no conviction/alumni
# check, no offering figures.  Production agreed: 42 of 516 Form D signals
# carried a filer_name.  Coverage is now the whole run, bounded by a ceiling.
#
# Measured 2026-07-26 against live EDGAR for a 7-day window: 1,172 Form D
# filings, 62/272 sampled across three pages passing the consumer gate (22.8%),
# i.e. ~270 consumer candidates.  500 leaves real headroom over that while
# still capping a 30-day manual backfill at ~100s of fetching.
#
# SEC publishes a 10 requests/second ceiling for automated clients.  We hold a
# deliberate 2x margin under it — other collectors in this same process also
# talk to sec.gov, and getting the fund's User-Agent blocked would cost far
# more than the handful of seconds a higher rate would save.
FORMD_MAX_WORKERS      = 5      # bounded pool — never an unbounded fan-out
FORMD_REQUESTS_PER_SEC = 5.0    # half of SEC's published 10 req/s limit
FORMD_ENRICH_CEILING   = 500    # hard cap on filings enriched in one run

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
    "feeder",                            # feeder fund (investment vehicle)
    "sidecar",                           # sidecar fund (investment co-invest structure)
    # Finance / banking / legal entities
    " financial",                        # financial services (Lizton Financial, etc.)
    "revocable trust",                   # estate planning trust
    " l p",                              # "L P" (LP with space between letters)
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
    # Medical / healthcare facilities (not consumer wellness brands)
    "surgery center", "surgical center",  # ambulatory surgical centers
    "car wash",                           # car wash franchises/facilities
    # Additional non-consumer patterns
    "industrial", "industries",           # manufacturing / industrial
    " dst",                               # Delaware Statutory Trust (real estate)
    "cooperative", " coop",               # farm / housing cooperatives
    "medical", "clinical", "hospital",    # healthcare (not consumer wellness)
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



# ─── Main service function ────────────────────────────────────────────────────

def _fetch_form_d_xml_content(adsh: str, cik: str) -> bytes | None:
    """
    Fetch Form D primary_doc.xml from EDGAR and return raw bytes.
    Returns None on HTTP error or network failure.
    Retries up to 3× with exponential backoff on 429 (SEC rate limit: 10 req/s).
    """
    if not adsh or not cik:
        return None
    adsh_clean = adsh.replace("-", "")
    cik_int = str(int(cik)) if cik.lstrip("0") else cik
    url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_int}/{adsh_clean}/primary_doc.xml"
    )
    import time as _time
    resp = None
    for attempt in range(3):
        resp = requests.get(
            url,
            headers={"User-Agent": "Bullish Stealth Finder research@bullish.co"},
            timeout=10,
        )
        if resp.status_code == 429:
            _time.sleep(2 ** attempt)
            continue
        break
    if resp is None or resp.status_code != 200:
        logger.debug("Form D XML fetch HTTP %d for %s", resp.status_code if resp else 0, adsh)
        return None
    return resp.content


def _parse_related_persons_from_xml(content: bytes) -> list[dict]:
    """Parse relatedPersonInfo blocks from Form D XML bytes."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        logger.debug("Form D XML parse error (persons): %s", exc)
        return []
    persons = []
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
    return persons


def parse_form_d_offering_data(content: bytes) -> dict | None:
    """
    Parse offeringData from Form D XML bytes.

    Looks for: totalOfferingAmount, totalAmountSold, minimumInvestmentAccepted.
    Returns:
        {
            "total_offering":       int | None,   # USD; None = unknown / not disclosed
            "amount_sold":          int | None,
            "minimum_investment":   int | None,
        }
    or None if the XML cannot be parsed or contains no offering figures.

    These are RANKING FEATURES only — never used to gate or exclude signals.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        logger.debug("Form D XML parse error (offering): %s", exc)
        return None

    result: dict[str, int | None] = {
        "total_offering":     None,
        "amount_sold":        None,
        "minimum_investment": None,
    }

    for elem in root.iter():
        local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        text  = (elem.text or "").strip()

        if local == "totalOfferingAmount":
            # Some filings mark indefinite raises with attribute indefiniteAmount="true"
            if elem.get("indefiniteAmount", "").lower() == "true":
                result["total_offering"] = None
            elif text and text.replace(".", "").lstrip("-").isdigit():
                val = int(float(text))
                result["total_offering"] = val if val > 0 else None

        elif local == "totalAmountSold":
            if text and text.replace(".", "").lstrip("-").isdigit():
                val = int(float(text))
                result["amount_sold"] = val if val > 0 else None

        elif local == "minimumInvestmentAccepted":
            if text and text.replace(".", "").lstrip("-").isdigit():
                val = int(float(text))
                result["minimum_investment"] = val if val > 0 else None

    if all(v is None for v in result.values()):
        return None
    return result


def fetch_form_d_related_persons(adsh: str, cik: str) -> list[dict]:
    """
    Fetch the EDGAR Form D XML filing and extract Related Persons.

    Form D XML stores every director, officer, and significant owner who was
    named in the filing — i.e. the #1–4 people who just made someone a legal
    promise.  This is the earliest structured record of who is building the brand.

    Args:
        adsh: EDGAR accession number e.g. "0001234567-24-000123"
        cik:  EDGAR CIK number (digits only)

    Called per-item by signal_pipeline.py after a signal is saved, so it can
    fire alongside the collection-time sweep. It therefore shares the same
    process-wide _SEC_PACER: the SEC limit applies to us as one client, and a
    fan-out of pipeline threads each fetching unpaced is exactly how a client
    gets blocked. In practice the pacer's slots are long expired by the time
    the pipeline runs, so this normally costs nothing.

    Returns:
        List of {"name": str, "relationships": list[str]}, empty list on failure.
    """
    _SEC_PACER.acquire()
    content = _fetch_form_d_xml_content(adsh, cik)
    if not content:
        return []
    try:
        persons = _parse_related_persons_from_xml(content)
        logger.debug("Form D %s: found %d related persons", adsh, len(persons))
        return persons
    except Exception as exc:
        logger.debug("Form D related persons parse failed for %s: %s", adsh, exc)
        return []


def _retry_after_seconds(resp, default: float) -> float:
    """
    Read a Retry-After header (delta-seconds form) off a 429, falling back to
    `default` when it is absent or not a plain number.  Clamped so a hostile or
    malformed header can never stall the nightly job.
    """
    raw = ""
    try:
        raw = (resp.headers.get("Retry-After") or "").strip()
    except Exception:          # headers missing / not a mapping
        return default
    try:
        wait = float(raw)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(wait, EDGAR_MAX_BACKOFF))


def _edgar_query(q: str, start_str: str, end_str: str, from_: int, size: int) -> dict:
    """
    Run one EDGAR full-text Form D query and return raw JSON.

    Retries up to EDGAR_MAX_ATTEMPTS times with exponential backoff (2s/4s/8s)
    on HTTP 5xx, HTTP 429 (honouring Retry-After), and on connection/timeout
    errors.  A 4xx other than 429 is our own bug, not SEC's — those raise on
    the first response with no retry.

    Raises requests.RequestException (HTTPError included) when every attempt
    fails, so the existing caller contract is unchanged.
    """
    params = {
        "q":         q,
        "forms":     "D",
        "dateRange": "custom",
        "startdt":   start_str,
        "enddt":     end_str,
        "from":      from_,
        "size":      size,
    }
    headers = {
        # SEC asks every automated client to identify itself — keep this.
        "User-Agent": "Bullish Stealth Finder research@bullish.co",
        "Accept":     "application/json",
    }

    for attempt in range(EDGAR_MAX_ATTEMPTS):
        is_last = attempt == EDGAR_MAX_ATTEMPTS - 1
        backoff = EDGAR_BACKOFF_BASE * (2 ** attempt)   # 2, 4, 8, …

        try:
            resp = requests.get(
                EDGAR_SEARCH_URL,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            if is_last:
                logger.warning(
                    "EDGAR query failed after %d attempts (network): %s",
                    EDGAR_MAX_ATTEMPTS, exc,
                )
                raise
            logger.warning(
                "EDGAR query network error (attempt %d/%d): %s — retrying in %.0fs",
                attempt + 1, EDGAR_MAX_ATTEMPTS, exc, backoff,
            )
            time.sleep(backoff)
            continue

        status = getattr(resp, "status_code", None)
        if status in EDGAR_RETRY_STATUSES and not is_last:
            wait = _retry_after_seconds(resp, backoff) if status == 429 else backoff
            logger.warning(
                "EDGAR query HTTP %s (attempt %d/%d) — retrying in %.0fs",
                status, attempt + 1, EDGAR_MAX_ATTEMPTS, wait,
            )
            time.sleep(wait)
            continue

        # Success, a non-retryable 4xx, or a retryable status on the final
        # attempt — raise_for_status() turns the last two into an HTTPError.
        resp.raise_for_status()
        return resp.json()

    # Unreachable: the loop always returns or raises.
    raise requests.RequestException("EDGAR query exhausted all attempts")


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

    # DE incorporation is a deliberate VC-track choice; give it a stronger signal.
    score_boost = 12 if inc_state == "DE" else 8

    return {
        "companyName": brand,
        "signal_type": "delaware",
        "category":    category,
        "score_boost": score_boost,
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
        # Internal-only: orders officer extraction when a run exceeds the
        # ceiling (_enrich_priority). Stripped before the signals are returned.
        "_inc_state":  inc_state,
    }


# ─── SEC request pacing ───────────────────────────────────────────────────────

class _RateLimiter:
    """
    Thread-safe request pacer: hands out at most `rate_per_sec` permits per
    second across every caller in this process.

    Implemented as a serialised next-slot clock rather than a token bucket, so
    there is no burst allowance — SEC never sees more than `rate_per_sec`
    requests inside any one-second window, which is what their published limit
    actually measures.  A rate of 0 disables pacing entirely.
    """

    def __init__(self, rate_per_sec: float):
        self.rate      = float(rate_per_sec)
        self._interval = (1.0 / self.rate) if self.rate > 0 else 0.0
        self._lock     = threading.Lock()
        self._next_at  = 0.0

    def acquire(self) -> None:
        """Block until this caller is allowed to issue its request."""
        if self._interval <= 0:
            return
        now = time.monotonic()
        with self._lock:
            slot          = max(now, self._next_at)
            self._next_at = slot + self._interval
        wait = slot - now
        if wait > 0:
            time.sleep(wait)


# Process-wide: the manual scan path and the nightly scheduler can overlap, and
# SEC's limit applies to us as one client, not per scan.
_SEC_PACER = _RateLimiter(FORMD_REQUESTS_PER_SEC)


def _fetch_form_d_xml_paced(sig: dict) -> tuple[dict, bytes | None]:
    """
    Worker body for the bounded fetch pool: wait for a rate-limit slot, then
    fetch one filing's XML.

    Returns (signal, content|None).  Every failure is swallowed into a None so
    one dead filing can never abort the rest of the batch — coverage degrades
    by one brand, not by the whole run.
    """
    try:
        _SEC_PACER.acquire()
        return sig, _fetch_form_d_xml_content(sig.get("_adsh", ""), sig.get("_cik", ""))
    except Exception as exc:
        logger.warning(
            "Form D XML fetch failed for %s: %s", sig.get("_adsh", "?"), exc
        )
        return sig, None


def _enrich_priority(sig: dict) -> tuple:
    """
    Sort key for officer extraction — lower sorts first.

    A normal run covers every filing, so this only bites when a run exceeds
    FORMD_ENRICH_CEILING.  When it does, Delaware-incorporated filings win:
    DE incorporation is the deliberate VC-track choice `_extract_signal`
    already rewards (score_boost 12 vs 8), so if we can only have some
    officers, those are the ones worth having.  Python's sort is stable, so
    EDGAR's own ordering is preserved inside each tier.
    """
    return (0 if (sig.get("_inc_state") or "").upper() == "DE" else 1,)


@lru_cache(maxsize=1)
def _person_name_check():
    """
    Resolve founder_discovery.looks_like_person once per process.

    Cached because a FAILED import is not cached by Python — without this, a
    missing founder_discovery would re-attempt the full import for every
    officer name on every filing.
    """
    try:
        from .founder_discovery import looks_like_person as _impl
        return _impl
    except ImportError:
        return None


def _looks_like_person(name: str) -> bool:
    """
    Person-name heuristic, with a local fallback when founder_discovery is
    unavailable (it pulls optional third-party deps at import time).
    """
    _impl = _person_name_check()
    if _impl is not None:
        return _impl(name)
    parts = name.strip().split()
    return (
        2 <= len(parts) <= 4
        and not re.search(r'\d', name)
        and not re.search(
            r'\b(LLC|Inc|Corp|Ltd|LLP|PLLC|LP|PC)\b', name, re.IGNORECASE
        )
        and name.strip().lower() not in {"n/a", "n/a n/a"}
    )


def _attach_form_d_xml_fields(sig: dict, content: bytes) -> None:
    """
    Parse one Form D XML payload and merge everything it yields onto `sig`.

    Always runs on the CALLING thread, never inside a fetch worker, so signal
    mutation and the conviction-before-alumni precedence rule stay
    deterministic no matter how the parallel fetches interleave.
    """
    from .conviction import check_conviction_match_multi
    from .exit_watch import check_exit_alumni_match_multi

    # ── Offering data ─────────────────────────────────────────────────────────
    # total_offering / amount_sold / minimum_investment are RANKING features.
    # minimum_investment in particular is a real tell ($25k reads very
    # differently from $1M) and scheduler.py persists it — keep it merged.
    offering = parse_form_d_offering_data(content)
    if offering:
        sig.update({k: v for k, v in offering.items() if v is not None})

    # ── Related persons ───────────────────────────────────────────────────────
    raw = _parse_related_persons_from_xml(content)
    if not raw:
        return

    enriched = []
    filer_name = None
    for person in raw:
        pname = person["name"]
        if pname.lower().startswith("n/a") or pname.strip().lower() == "n/a":
            continue
        p_conv   = check_conviction_match_multi([pname])
        p_alumni = check_exit_alumni_match_multi([pname]) if not p_conv else None
        enriched.append({
            **person,
            "conviction_match":  p_conv,
            "exit_alumni_match": p_alumni,
        })
        if filer_name is None and _looks_like_person(pname):
            filer_name = pname

    sig["related_persons"] = enriched
    if filer_name:
        sig["filer_name"] = filer_name
    # Bubble up first conviction match so enrichment.py can use it directly
    _top_conv = next(
        (p["conviction_match"] for p in enriched if p.get("conviction_match")), None
    )
    if _top_conv:
        sig["conviction_match"] = _top_conv

    # No conviction founder on this filing — bubble up the first exit alumni
    # match instead, so scheduler.py can fire the immediate ALUMNI watchlist
    # alert (it reads exit_alumni_match at the signal TOP level, not inside
    # related_persons, so per-person values alone left that alert unreachable).
    #
    # The two designations are separate and must never be collapsed:
    # conviction outranks alumni, and the conviction scan runs across the WHOLE
    # officer list before alumni is even considered — an alumni listed above a
    # conviction founder must never win. Per-person values stay on
    # related_persons either way and are unaffected.
    if not sig.get("conviction_match"):
        _top_alumni = next(
            (p["exit_alumni_match"] for p in enriched if p.get("exit_alumni_match")),
            None,
        )
        if _top_alumni:
            sig["exit_alumni_match"] = _top_alumni


def _enrich_related_persons(
    signals: list,
    limit: int = FORMD_ENRICH_CEILING,
) -> None:
    """
    Fetch the EDGAR XML ONCE per Form D filing and attach related_persons,
    filer_name, and offering data in-place.

    COVERAGE: `limit` is a safety ceiling, not a sampling budget.  It defaults
    to FORMD_ENRICH_CEILING so a realistic run (~270 consumer candidates from a
    7-day window, measured) gets EVERY filing's officers rather than an
    arbitrary first 50.  A filing skipped here is a founder the product never
    sees.

    THROTTLE: fetches run on a bounded pool of at most FORMD_MAX_WORKERS
    threads, paced process-wide by _SEC_PACER at FORMD_REQUESTS_PER_SEC — half
    of SEC's published 10 req/s ceiling.  Only the HTTP fetch is parallel;
    parsing and watchlist matching happen on the calling thread, so the
    conviction-before-alumni precedence rule cannot be raced.

    COST: ~270 filings at 5 req/s is ~55s of wall clock, against a nightly full
    scan of roughly 75 minutes (~1.2%).  The old 50-filing serial loop cost
    ~22s, so this buys 5x the coverage for ~33s.

    ORDERING: when a run genuinely exceeds the ceiling, Delaware-incorporated
    filings are covered first (see _enrich_priority) instead of whatever order
    EDGAR happened to return.

    Sets on each signal (mutates in place):
        related_persons: list[{"name": str, "relationships": list[str],
                               "conviction_match": dict|None,
                               "exit_alumni_match": dict|None}]
        filer_name: str|None  — first person-looking related person
        conviction_match: dict — first per-person conviction match, promoted
        exit_alumni_match: dict — first per-person alumni match, promoted ONLY
                                  when no conviction match was promoted
        total_offering: int|None   — USD raise target from offeringData
        amount_sold: int|None      — USD amount sold so far
        minimum_investment: int|None
    """
    try:
        budget = int(limit)
    except (TypeError, ValueError):
        budget = FORMD_ENRICH_CEILING
    budget = max(0, min(budget, FORMD_ENRICH_CEILING))
    if budget <= 0:
        return

    # Only filings that carry EDGAR IDs are fetchable. Selecting these BEFORE
    # applying the budget matters: the old signals[:limit] slice spent budget
    # on signals it then skipped for a missing adsh.
    targets = [s for s in signals if s.get("_adsh") and s.get("_cik")]
    eligible = len(targets)
    if not eligible:
        return

    targets.sort(key=_enrich_priority)      # stable — DE first, EDGAR order within tier
    if eligible > budget:
        logger.warning(
            "Form D officer extraction capped at %d of %d eligible filings — "
            "%d skipped (Delaware-incorporated prioritised)",
            budget, eligible, eligible - budget,
        )
        targets = targets[:budget]

    workers = max(1, min(FORMD_MAX_WORKERS, len(targets)))
    logger.info(
        "Form D officer extraction: %d filings on %d workers at %.1f req/s "
        "(~%.0fs)",
        len(targets), workers, FORMD_REQUESTS_PER_SEC,
        len(targets) / FORMD_REQUESTS_PER_SEC if FORMD_REQUESTS_PER_SEC else 0,
    )

    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="formd-xml") as pool:
        futures = [pool.submit(_fetch_form_d_xml_paced, s) for s in targets]
        # Consumed in submit order, not completion order, so the mutation
        # sequence is identical to the old serial loop's.
        for future in futures:
            sig, content = future.result()
            if not content:
                continue
            try:
                _attach_form_d_xml_fields(sig, content)
            except Exception as exc:
                # One malformed filing must not cost the rest of the batch.
                logger.warning(
                    "Form D XML parse failed for %s: %s", sig.get("_adsh", "?"), exc
                )


def search_recent_delaware_entities(
    days_back: int = 7,
    max_results: int = 2000,
    max_filings: int = 6000,
    related_persons_limit: int = FORMD_ENRICH_CEILING,
) -> dict:
    """
    Fetch recent Form D filings from consumer companies via SEC EDGAR (free,
    no API key required) — all 50 US states.

    Strategy:
    BROAD SWEEP — probe for the true Form D count in the window, then page
    through the FULL universe (up to max_filings). Measured 2026-07-26 against
    live EDGAR: a 7-day window held 1,172 Form Ds and 22.8% of a 272-filing
    sample passed the consumer gate (_FUND_ITEMS + blocklist), i.e. ~270
    consumer brand candidates — materially more than the ~110 this docstring
    used to claim, which is part of why the old first-50 officer cap was
    covering so little. max_results=2000 ensures we never hit the cap and miss
    brands in any realistic scan window.
    Targeted keyword queries were tried and removed: EDGAR full-text search
    matches officer names and addresses, not just company names, so terms
    like "food" or "beauty" return near-zero results and add no signal.
    Invented-name brands (Fascent, Rivalz, Mosh) are only findable by
    sweeping the full universe with _is_consumer_candidate filtering.

    Domain corroboration is handled by CT logs (ctlogs.py) — not DomainsDB.

    RESILIENCE: every EDGAR call retries with backoff (_edgar_query). If the
    count probe still fails, we do not bail out — we sweep a fixed
    EDGAR_PROBE_FALLBACK_FILINGS-deep page range instead, so an SEC outage
    costs coverage rather than the whole night's Form D collection.

    Returns:
        {
            "signals":      list of signal dicts,
            "total_found":  int (filings in window; a swept-count FLOOR when
                                 the count probe was unavailable),
            "fetched":      int (Form D signals only),
            "error":        str | None (set only on a mid-sweep page failure;
                                 partial signals are still returned),
        }

    Each Form D signal dict may carry:
        related_persons: list of {name, relationships, conviction_match, exit_alumni_match}
        filer_name: str — first person-looking related person (skips SerpAPI in founder discovery)
    """
    end_dt    = datetime.utcnow()
    start_dt  = end_dt - timedelta(days=days_back)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str   = end_dt.strftime("%Y-%m-%d")

    signals      = []
    total_found  = 0
    seen_names   = set()
    page_size    = 100
    error        = None
    probe_failed = False
    raw_seen     = 0          # filings actually swept — the total_found floor

    # ── Probe: discover true Form D count in this window ──────────────────────
    try:
        probe = _edgar_query("", start_str, end_str, 0, 1)
        try:
            total_found = int(probe.get("hits", {}).get("total", {}).get("value", 0))
        except (TypeError, ValueError):
            total_found = 0
        # A 200 is not proof the probe was USEFUL. If the payload shape changes,
        # or an error page comes back as JSON, total_found lands on 0 — and 0
        # means pages_broad == 0, so the sweep never runs and the night is zeroed
        # while the run still looks clean. Any real multi-day window holds
        # thousands of Form Ds, so treat a zero as untrustworthy and sweep
        # anyway; if the window genuinely is empty the first page returns no
        # hits and the loop breaks immediately, costing one request.
        if total_found <= 0:
            probe_failed = True
            logger.warning(
                "EDGAR count probe returned an unusable total (%r) despite HTTP 200 "
                "— falling back to a blind sweep",
                probe.get("hits", {}).get("total"),
            )
    except requests.RequestException as exc:
        # Probe outage must degrade coverage, not zero it.  We do NOT set
        # "error" here: callers treat a non-null error as "discard this
        # collection", and a blind sweep still returns real Form D signals.
        probe_failed = True
        logger.warning(
            "EDGAR count probe failed after retries (%s) — falling back to a "
            "blind sweep of up to %d filings",
            exc, min(EDGAR_PROBE_FALLBACK_FILINGS, max_filings),
        )

    # ── Broad sweep: cover the full universe up to max_filings ────────────────
    pages_cap = (max_filings + page_size - 1) // page_size
    if probe_failed:
        # Unknown total: sweep a sane fixed depth and let the empty-page break
        # below stop us early if the window is smaller than the fallback.
        fallback_filings = min(EDGAR_PROBE_FALLBACK_FILINGS, max_filings)
        pages_broad      = max(1, (fallback_filings + page_size - 1) // page_size)
    else:
        pages_needed = (total_found + page_size - 1) // page_size
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
        raw_seen += len(raw_hits)

        for hit in raw_hits:
            if len(signals) >= max_results:
                break
            sig = _extract_signal(hit.get("_source", {}), seen_names, end_dt)
            if sig:
                seen_names.add(sig["_name"])
                signals.append(sig)

        time.sleep(0.5)

    # ── Related persons: fetch XML + match watchlist for the WHOLE run ────────
    # related_persons_limit is a ceiling, not a sample size: at the default it
    # covers every consumer candidate a realistic window produces. Officers are
    # the highest-value field here, so an uncovered filing is a missed founder.
    formd_signals = list(signals)
    if related_persons_limit > 0:
        _enrich_related_persons(formd_signals, limit=related_persons_limit)

    # Strip internal-only keys before returning
    for s in signals:
        s.pop("_name", None)
        s.pop("_inc_state", None)

    # Probe outage: report the filings we actually saw as the total floor
    # rather than a bare 0, so the number never reads as "nothing was filed".
    if probe_failed:
        total_found = raw_seen

    return {
        "signals":     signals,
        "total_found": total_found,
        "fetched":     len(formd_signals),
        "error":       error,
    }
