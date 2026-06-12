"""
exit_watch.py
-------------
Tracks two things:

1. EXIT_ALUMNI — hand-curated early operators (#2–4) from notable consumer exits who
   are NOT already in the conviction founders list. Matched by name against signal text
   (same mechanism as conviction.py). Gives a +8 boost and an ALUMNI badge in the UI.

2. EXIT_WATCH_BRANDS — notable acquired/pivoted consumer brands whose alumni are
   worth flagging when found in future signals. Auto-populated from acquisition
   language detected in press/newswire signals; persisted in Redis.

HOW TO ADD AN ALUMNI:
  Add to EXIT_ALUMNI below. Format is identical to conviction.py:
    "Firstname Lastname": ("one-line reason", ["Known Brand 1", "Known Brand 2"]),
  Both first and last name must appear in signal text (prevents false matches).

HOW TO ADD A WATCHED BRAND:
  Add to EXIT_WATCH_BRANDS below. The dict is merged with any dynamically detected
  brands stored in Redis. Used by check_exit_alumni(past_companies) after a Proxycurl
  lookup returns a person's work history.
"""
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# ── Early operators / alumni to match by name ─────────────────────────────────
# People who were #2–4 at notable exits but aren't in the main conviction list.
# Match logic is identical to conviction.py — both first AND last name must appear.

EXIT_ALUMNI: dict[str, tuple[str, list[str]]] = {

    # ── Glossier ──────────────────────────────────────────────────────────────
    "Bryan Mahoney":    ("Glossier CTO; built the tech stack behind a $1B DTC brand", ["Glossier"]),

    # ── Peloton ───────────────────────────────────────────────────────────────
    "Robin Arzón":      ("Peloton VP Fitness Programming; the most recognizable face of the brand outside the CEO", ["Peloton"]),

    # ── Away ──────────────────────────────────────────────────────────────────
    "Selby Drummond":   ("Away Head of Brand (former Vogue editor); early brand architect at Away", ["Away"]),

    # ── Bala Weights ──────────────────────────────────────────────────────────
    "Natalie Holloway": ("Bala Weights co-founder; design-forward fitness accessories brand, Shark Tank success", ["Bala"]),
    "Max Kislevitz":    ("Bala Weights co-founder; premium fitness accessories — Shark Tank deal", ["Bala"]),

    # ── Media / community founders with DTC crossover ─────────────────────────
    "Jaclyn Johnson":   ("Create & Cultivate founder; female entrepreneurship media + events brand", ["Create & Cultivate"]),
    "Amanda Goetz":     ("House of Wise founder; ex-TheKnot.com VP Marketing turned wellness brand founder", ["House of Wise"]),

    # ── Add more here ──────────────────────────────────────────────────────────
    # "Firstname Lastname": ("reason", ["Brand"]),
}

# ── Brands whose alumni are worth tracking ────────────────────────────────────
# When we have Proxycurl data for a person (past_companies list), we check
# if they worked at any of these brands. Auto-detects from press signals too.

EXIT_WATCH_BRANDS: dict[str, dict] = {
    "Mirror":            {"acquirer": "lululemon",         "year": 2020, "notes": "$500M acquisition"},
    "Casper":            {"acquirer": "Durational Capital", "year": 2022, "notes": "went private at discount"},
    "Away":              {"acquirer": None,                "year": None, "notes": "leadership changes; alumni dispersed"},
    "Outdoor Voices":    {"acquirer": None,                "year": None, "notes": "struggled; founder out 2020"},
    "Glossier":          {"acquirer": None,                "year": None, "notes": "major layoffs 2022; many alumni now free"},
    "Daily Harvest":     {"acquirer": None,                "year": None, "notes": "leadership changes post-recall"},
    "RxBar":             {"acquirer": "Kellogg's",         "year": 2017, "notes": "$600M acquisition"},
    "Plated":            {"acquirer": "Albertsons",        "year": 2017, "notes": "~$200M acquisition"},
    "Birchbox":          {"acquirer": "FemBo Holdings",    "year": 2021, "notes": "sold out of bankruptcy"},
    "Bonobos":           {"acquirer": "Walmart",           "year": 2017, "notes": "$310M acquisition"},
    "Dollar Shave Club": {"acquirer": "Unilever",          "year": 2016, "notes": "$1B acquisition"},
    "Honest Tea":        {"acquirer": "Coca-Cola",         "year": 2011, "notes": "full acquisition 2011"},
    "Hu Kitchen":        {"acquirer": "Mondelez",          "year": 2021, "notes": "premium chocolate acquisition"},
    "Brandless":         {"acquirer": None,                "year": None, "notes": "shut down 2019; alumni watching"},
    "Foxtrot":           {"acquirer": None,                "year": None, "notes": "shut down April 2024; many alumni now free"},
    "Peloton":           {"acquirer": None,                "year": None, "notes": "public; major leadership changes 2022–23"},
    "Sweetgreen":        {"acquirer": None,                "year": None, "notes": "public; founding team watching"},
    "Thrive Market":     {"acquirer": None,                "year": None, "notes": "leadership watching"},
    "KRAVE Jerky":       {"acquirer": "Hershey",           "year": 2015, "notes": "better-snacking acquisition"},
    "Adore Me":          {"acquirer": "Walmart",           "year": 2022, "notes": "~$400M acquisition"},
    "Yes To":            {"acquirer": None,                "year": None, "notes": "various PE owners; team diaspora"},
    "Nasty Gal":         {"acquirer": "Boohoo",            "year": 2017, "notes": "sold out of bankruptcy; brand lives on"},
    "Greats":            {"acquirer": "Steve Madden",      "year": 2019, "notes": "sneakers DTC acquisition"},
    "Bala":              {"acquirer": None,                "year": None, "notes": "Shark Tank; strong brand, watching alumni"},
    "Ollie":             {"acquirer": None,                "year": None, "notes": "premium pet food; series B+ watching"},
    "Good Culture":      {"acquirer": None,                "year": None, "notes": "modern protein brand; alumni watching"},
}

# Redis key for dynamically detected exit brands
_REDIS_KEY = "exit_watch:brands"


# ── Redis helpers ─────────────────────────────────────────────────────────────

def _get_redis():
    """Return a Redis client or None if REDIS_URL is not configured."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return None
    try:
        import redis
        return redis.Redis.from_url(url, decode_responses=True, socket_timeout=2)
    except Exception as exc:
        logger.debug("exit_watch: Redis unavailable: %s", exc)
        return None


def get_exit_brands() -> dict[str, dict]:
    """
    Return the combined exit watch brand list (static dict + Redis-stored dynamic entries).
    """
    result = dict(EXIT_WATCH_BRANDS)
    try:
        r = _get_redis()
        if r:
            raw = r.get(_REDIS_KEY)
            if raw:
                result.update(json.loads(raw))
    except Exception as exc:
        logger.debug("exit_watch: could not load dynamic brands from Redis: %s", exc)
    return result


def add_exit_brand(
    brand_name: str,
    acquirer: str = None,
    year: int = None,
    notes: str = "",
) -> None:
    """
    Persist a dynamically detected exit brand to Redis.
    Called when acquisition language is found in a press or newswire signal.
    """
    if not brand_name or not brand_name.strip():
        return
    normalized = brand_name.strip().title()
    try:
        r = _get_redis()
        if not r:
            return
        existing_raw = r.get(_REDIS_KEY)
        existing = json.loads(existing_raw) if existing_raw else {}
        existing[normalized] = {
            "acquirer": acquirer,
            "year": year,
            "notes": notes,
            "auto_detected": True,
        }
        r.set(_REDIS_KEY, json.dumps(existing))
        logger.info("exit_watch: added '%s' to dynamic brand list", normalized)
    except Exception as exc:
        logger.warning("exit_watch: could not persist brand '%s': %s", brand_name, exc)


# ── Name-based matching (like conviction.py) ──────────────────────────────────

def check_exit_alumni_match(text: str) -> dict | None:
    """
    Check if any exit alumni name appears in the given text.
    Returns {"name": str, "reason": str, "known_brands": list[str]} or None.
    """
    if not text:
        return None
    text_lower = text.lower()
    for full_name, (reason, known_brands) in EXIT_ALUMNI.items():
        parts = full_name.strip().split()
        if len(parts) < 2:
            continue
        first = parts[0].lower()
        last  = parts[-1].lower()
        if first in text_lower and last in text_lower:
            logger.info("Exit alumni match: '%s' found in signal text", full_name)
            return {"name": full_name, "reason": reason, "known_brands": known_brands}
    return None


def check_exit_alumni_match_multi(texts: list[str]) -> dict | None:
    """Check multiple text fields; return first alumni match found."""
    for text in texts:
        match = check_exit_alumni_match(text or "")
        if match:
            return match
    return None


# ── Post-Proxycurl company history check ─────────────────────────────────────

def check_exit_alumni(past_companies: list[str]) -> dict | None:
    """
    After a Proxycurl lookup, check if any company in the person's work history
    matches the exit watch brand list.

    Args:
        past_companies: list of company names from LinkedIn work history

    Returns:
        {"brand": str, "acquirer": str|None, "notes": str} or None
    """
    if not past_companies:
        return None
    brands = get_exit_brands()
    for company in past_companies:
        company_lower = company.lower().strip()
        for brand, info in brands.items():
            brand_lower = brand.lower()
            if brand_lower in company_lower or company_lower in brand_lower:
                return {
                    "brand":    brand,
                    "acquirer": info.get("acquirer"),
                    "notes":    info.get("notes", ""),
                }
    return None


# ── Acquisition detection from press text ─────────────────────────────────────

# Patterns that suggest an acquisition announcement
_ACQ_PATTERNS = [
    r'([A-Z][A-Za-z\s]{2,25}?)\s+(?:has been|was|is)\s+acquired\s+by\b',
    r'\bacquires?\s+([A-Z][A-Za-z\s]{2,25}?)(?:\s+for|\s+in|\.|,)',
    r'\bacquisition\s+of\s+([A-Z][A-Za-z\s]{2,25?})(?:\s+for|\s+by|\.|,)',
    r'([A-Z][A-Za-z\s]{2,25}?)\s+sold\s+to\s+[A-Z][A-Za-z]',
]
_ACQ_RE = [re.compile(p) for p in _ACQ_PATTERNS]

# Words that indicate it's not a consumer brand acquisition
_ACQ_EXCLUDE = re.compile(
    r'\b(stake|shares|real estate|property|land|patent|technology platform|'
    r'software|SaaS|enterprise|B2B|infrastructure|facility|warehouse)\b',
    re.IGNORECASE,
)


def detect_acquisition_in_text(text: str) -> str | None:
    """
    Scan text for consumer brand acquisition language.
    Returns the acquired brand name if found, else None.
    """
    if not text:
        return None
    if _ACQ_EXCLUDE.search(text):
        return None
    for pattern in _ACQ_RE:
        match = pattern.search(text)
        if match:
            brand = match.group(1).strip().rstrip(".,")
            if 3 <= len(brand) <= 35:
                return brand
    return None
