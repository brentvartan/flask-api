"""
press_stealth.py
----------------
Startup press RSS scanner — surfaces founders actively building in stealth
before they've announced their brand.

Unlike newswire.py (which catches brand-published press releases), this service
monitors editorial/journalist-written press for the specific language pattern
of a founder who is "building something new" but hasn't named it yet.

Lead time: TechCrunch covered Brynn Putnam's stealth hardware startup in Nov 2024
— 19 months before the Board Series A announcement. This is the signal type
that catches that.

Pattern-keyed, not person-keyed: we look for stealth language in articles,
not specific founder names. The conviction list then links if a known name appears.

No API keys required. Public RSS feeds only.
"""
import re
import json
import logging
import xml.etree.ElementTree as ET
import urllib.request
import urllib.error

from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20

# ── RSS feeds ────────────────────────────────────────────────────────────────
# Editorial / journalist-written startup press.
# Deliberately broader than newswire — we want journalist-surfaced stealth,
# not brand-published releases.

FEEDS = [
    # Startup / VC press
    ("techcrunch",      "https://techcrunch.com/category/startups/feed/"),
    ("techcrunch",      "https://techcrunch.com/feed/"),
    ("crunchbase_news", "https://news.crunchbase.com/feed/"),
    ("venturebeat",     "https://venturebeat.com/feed/"),

    # Consumer / brand trade press
    ("modern_retail",   "https://www.modernretail.co/feed/"),
    ("glossy",          "https://www.glossy.co/feed/"),
    ("beauty_independent", "https://www.beautyindependent.com/feed/"),
    ("food_dive",       "https://www.fooddive.com/feeds/news/"),
    ("food_navigator",  "https://www.foodnavigator-usa.com/Info/RSS-Feeds/RSS-Content"),
]

# ── Stealth-signal phrase patterns ───────────────────────────────────────────
# These are the phrases a journalist uses when covering a founder who is
# "building something new" before naming it publicly.
# Match ANY of these in title OR description.

STEALTH_PATTERNS = [
    r"\bstealth\b",
    r"building something new",
    r"building in stealth",
    r"unannounced startup",
    r"quietly building",
    r"left .{3,40} to (build|found|launch|start)",
    r"(left|leaving) .{3,40} (to build|to found|to launch)",
    r"something new in (consumer|food|bev|beauty|wellness|health|fitness|pet|apparel|fashion|CPG|DTC)",
    r"stealth (consumer|brand|startup|mode|company|venture)",
    r"(founder|co-founder).{0,60}(stealth|building|secret|unannounced)",
    r"(stealth|secret|unannounced).{0,60}(brand|product|company|startup|launch)",
    r"incubating.{0,40}(brand|company|startup|product)",
    r"pre-?launch.{0,40}(consumer|brand|CPG|DTC|food|beauty|wellness)",
    r"new (consumer|brand|CPG|DTC|food|beauty|wellness).{0,40}(venture|startup|company)",
]

_STEALTH_RE = re.compile("|".join(STEALTH_PATTERNS), re.IGNORECASE)

# ── Consumer context filter ──────────────────────────────────────────────────
# Article must contain at least one consumer signal word to avoid B2B stealth.
# (We don't want "stealth cybersecurity startup" or "stealth defense contractor")

CONSUMER_CONTEXT_WORDS = [
    "consumer", "brand", "DTC", "CPG", "food", "beverage", "drink", "snack",
    "beauty", "skincare", "wellness", "health", "fitness", "apparel", "fashion",
    "pet", "lifestyle", "retail", "ecommerce", "e-commerce", "direct-to-consumer",
    "supplement", "nutrition", "personal care", "grooming", "home", "fragrance",
    "Glossier", "Olipop", "Liquid Death", "Harry's", "Warby", "RxBar", "RXBAR",
    "L'Oréal", "Unilever", "P&G", "Procter", "PepsiCo", "Mondelez", "Nestlé",
    "lululemon", "Nike", "Peloton", "Mirror",
]

_CONSUMER_RE = re.compile(
    "|".join(re.escape(w) for w in CONSUMER_CONTEXT_WORDS),
    re.IGNORECASE,
)

# ── Known B2B/enterprise exclusion patterns ──────────────────────────────────
# Hard-exclude if title contains these — even if consumer words also present

B2B_EXCLUSION_PATTERNS = [
    r"\benterp?rise\b", r"\bB2B\b", r"\bSaaS\b", r"\bAPIb\b",
    r"\bcybersecurity\b", r"\bdefense\b", r"\bgovernment\b",
    r"\binfrastructure\b", r"\bdevops\b", r"\bfintech\s+platform\b",
    r"\blocgistics\b", r"\bprocurement\b",
]
_B2B_RE = re.compile("|".join(B2B_EXCLUSION_PATTERNS), re.IGNORECASE)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    """Remove HTML tags from text."""
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def _parse_date(date_str: str) -> datetime | None:
    """Parse RSS date string to datetime, return None on failure."""
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _extract_brand_hint(title: str, description: str) -> str:
    """
    Try to extract a brand name hint from the article.
    Falls back to the article title (truncated) if no brand found.
    """
    # Look for quoted brand names or capitalized proper nouns following "called", "named", "dubbed"
    for pattern in [
        r'(?:called|named|dubbed|branded|called)\s+"([^"]{2,30})"',
        r'"([A-Z][a-z]{2,20}(?:\s[A-Z][a-z]{2,20})?)"',
    ]:
        m = re.search(pattern, title + " " + description)
        if m:
            return m.group(1).strip()

    # Fall back to truncated title (strip common article prefixes)
    clean = re.sub(r'^(Exclusive:|Scoop:|Breaking:|Report:)\s*', '', title, flags=re.IGNORECASE)
    return clean[:60].strip()


def _fetch_feed(source: str, url: str, cutoff: datetime) -> list[dict]:
    """Fetch one RSS feed and return matching stealth articles."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "BullishStealthFinder/1.0 (contact: brent@bullish.co)",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            xml_bytes = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        logger.warning("Press stealth: feed '%s' error: %s", source, e)
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.warning("Press stealth: XML parse error for '%s': %s", source, e)
        return []

    items = root.findall(".//item")
    results = []

    for item in items:
        title       = _strip_html(item.findtext("title") or "")
        description = _strip_html(item.findtext("description") or "")
        link        = (item.findtext("link") or "").strip()
        pub_date    = item.findtext("pubDate") or item.findtext("dc:date", namespaces={"dc": "http://purl.org/dc/elements/1.1/"}) or ""

        # Date filter
        parsed_date = _parse_date(pub_date)
        if parsed_date and parsed_date.tzinfo is None:
            parsed_date = parsed_date.replace(tzinfo=timezone.utc)
        if parsed_date and parsed_date < cutoff:
            continue

        full_text = f"{title} {description}"

        # Hard B2B exclusion on title
        if _B2B_RE.search(title):
            continue

        # Must match a stealth phrase
        if not _STEALTH_RE.search(full_text):
            continue

        # Must have consumer context
        if not _CONSUMER_RE.search(full_text):
            continue

        brand_hint = _extract_brand_hint(title, description)
        timestamp  = parsed_date.isoformat() if parsed_date else datetime.now(timezone.utc).isoformat()

        results.append({
            "companyName":  brand_hint,
            "signal_type":  "press_stealth",
            "category":     "Unknown",
            "score_boost":  10,
            "description":  f"{title[:120]} [{source}]",
            "url":          link,
            "notes":        (
                f"Journalist-written article with stealth-founder language in {source}. "
                f"Title: {title}. "
                f"This article may surface a founder building in stealth before any brand announcement. "
                f"Check article for founder name and brand context."
            ),
            "timestamp":    timestamp,
            "source":       source,
        })

    return results


def search_recent_press_stealth(days_back: int = 14, max_results: int = 50) -> dict:
    """
    Scan startup + consumer trade press RSS for stealth-founder articles.

    Args:
        days_back:   Look back this many days (1–30)
        max_results: Maximum signals to return

    Returns:
        {
            "signals": [...],
            "total_found": int,
            "fetched": int,
            "error": str | None,
        }
    """
    days_back   = max(1, min(days_back, 30))
    max_results = max(1, min(max_results, 100))

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    all_signals = []
    errors      = []
    seen_urls   = set()

    for source, url in FEEDS:
        try:
            items = _fetch_feed(source, url, cutoff)
            for item in items:
                if item["url"] not in seen_urls:
                    seen_urls.add(item["url"])
                    all_signals.append(item)
        except Exception as exc:
            errors.append(f"{source}: {exc}")
            logger.warning("Press stealth feed error (%s): %s", source, exc)

    # Sort by timestamp descending
    all_signals.sort(key=lambda s: s.get("timestamp", ""), reverse=True)
    signals = all_signals[:max_results]

    return {
        "signals":     signals,
        "total_found": len(all_signals),
        "fetched":     len(signals),
        "error":       errors[0] if errors and not signals else None,
    }
