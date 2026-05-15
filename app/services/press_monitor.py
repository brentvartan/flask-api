"""
Consumer trade press cross-reference layer for Stealth Finder.

Monitors ~20 RSS feeds across Beauty, Health/Wellness, Food/Beverage, Apparel,
Pet, and DTC/Retail verticals.  Matches article titles/summaries against a list
of brand names already in the DB.  Used as a validation signal, not a discovery
source — if the press is writing about a brand we spotted in stealth, that
corroborates our signal.

Uses only stdlib (urllib + xml.etree) — no feedparser dependency.
"""
import re
import logging
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

_UA = "Bullish Stealth Finder press-monitor/1.0 research@bullish.co"
_TIMEOUT = 15
_MAX_ARTICLES_PER_FEED = 50

# ─── Feed registry ────────────────────────────────────────────────────────────

PRESS_FEEDS = [
    # (publication_name, rss_url, vertical)
    # Beauty
    ("BeautyMatter",         "https://beautymatter.com/feed/",                        "Beauty"),
    ("Beauty Independent",   "https://www.beautyindependent.com/feed/",               "Beauty"),
    ("Glossy",               "https://www.glossy.co/feed/",                           "Beauty"),
    ("Happi",                "https://www.happi.com/rss/happi.xml",                   "Beauty"),
    ("CosmeticsDesign-USA",  "https://www.cosmeticsdesign-usa.com/rss/news",          "Beauty"),
    # Health / Wellness
    ("Well+Good",            "https://www.wellandgood.com/feed/",                     "Health/Wellness"),
    ("New Hope Network",     "https://www.newhope.com/rss.xml",                       "Health/Wellness"),
    ("NutraIngredients-USA", "https://www.nutraingredients-usa.com/rss/topnews.xml",  "Health/Wellness"),
    ("Nutritional Outlook",  "https://www.nutritionaloutlook.com/rss/articles",       "Health/Wellness"),
    # Food / Beverage
    ("BevNET",               "https://www.bevnet.com/feed/",                          "CPG/Food/Drink"),
    ("NOSH",                 "https://www.nosh.com/feed/",                            "CPG/Food/Drink"),
    ("Food Dive",            "https://www.fooddive.com/feeds/news/",                  "CPG/Food/Drink"),
    ("Grocery Dive",         "https://www.grocerydive.com/feeds/news/",               "CPG/Food/Drink"),
    # Apparel / Fashion
    ("Fashionista",          "https://fashionista.com/feed",                          "Apparel"),
    ("Business of Fashion",  "https://www.businessoffashion.com/rss",                 "Apparel"),
    ("WWD",                  "https://wwd.com/feed/",                                 "Apparel"),
    # Pet
    ("Pet Business",         "https://www.petbusiness.com/rss",                       "Pet"),
    ("Pet Product News",     "https://www.petproductnews.com/rss",                    "Pet"),
    # DTC / Retail
    ("Modern Retail",        "https://www.modernretail.co/feed/",                     "DTC/Retail"),
    ("Retail Dive",          "https://www.retaildive.com/feeds/news/",                "DTC/Retail"),
    ("Fast Company",         "https://www.fastcompany.com/feed/latest",               "DTC/Retail"),
]


# ─── RSS helpers ──────────────────────────────────────────────────────────────

def _fetch_feed(url: str) -> list[dict]:
    """Fetch one RSS/Atom feed and return a list of article dicts."""
    try:
        req = Request(url, headers={"User-Agent": _UA})
        with urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read(512 * 1024)  # cap at 512 KB
    except (HTTPError, URLError, OSError, Exception) as exc:
        logger.debug("Feed fetch failed for %s: %s", url, exc)
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        logger.debug("Feed parse error for %s: %s", url, exc)
        return []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    articles = []

    # RSS 2.0
    for item in root.iter("item"):
        title   = (item.findtext("title") or "").strip()
        link    = (item.findtext("link") or "").strip()
        summary = (item.findtext("description") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        if title or link:
            articles.append({
                "title":   title,
                "url":     link,
                "summary": re.sub(r"<[^>]+>", " ", summary)[:500].strip(),
                "date":    _parse_date(pub_date),
            })
        if len(articles) >= _MAX_ARTICLES_PER_FEED:
            break

    # Atom (if no RSS items found)
    if not articles:
        for entry in root.findall("atom:entry", ns):
            title   = (entry.findtext("atom:title", namespaces=ns) or "").strip()
            link_el = entry.find("atom:link", ns)
            link    = (link_el.get("href", "") if link_el is not None else "").strip()
            summary = (entry.findtext("atom:summary", namespaces=ns) or "").strip()
            pub_date = (entry.findtext("atom:updated", namespaces=ns) or "").strip()
            if title or link:
                articles.append({
                    "title":   title,
                    "url":     link,
                    "summary": re.sub(r"<[^>]+>", " ", summary)[:500].strip(),
                    "date":    _parse_date(pub_date),
                })
            if len(articles) >= _MAX_ARTICLES_PER_FEED:
                break

    return articles


def _parse_date(raw: str) -> str:
    """Best-effort date extraction → 'YYYY-MM-DD' or ''."""
    if not raw:
        return ""
    m = re.search(r"\d{4}-\d{2}-\d{2}", raw)
    if m:
        return m.group(0)
    # Try to parse RFC-2822 dates like 'Mon, 15 Jan 2024 08:00:00 GMT'
    m2 = re.search(r"(\d{1,2})\s+(\w{3})\s+(\d{4})", raw)
    if m2:
        months = {
            "jan": "01", "feb": "02", "mar": "03", "apr": "04",
            "may": "05", "jun": "06", "jul": "07", "aug": "08",
            "sep": "09", "oct": "10", "nov": "11", "dec": "12",
        }
        mo = months.get(m2.group(2).lower(), "01")
        return f"{m2.group(3)}-{mo}-{m2.group(1).zfill(2)}"
    return ""


# ─── Brand matching ───────────────────────────────────────────────────────────

_MIN_BRAND_LEN = 4  # ignore very short names to avoid noise


def _matches(brand_name: str, text: str) -> bool:
    """True if brand_name appears as a whole word/phrase in text."""
    if len(brand_name) < _MIN_BRAND_LEN or not text:
        return False
    try:
        return bool(re.search(r"\b" + re.escape(brand_name) + r"\b", text, re.IGNORECASE))
    except Exception:
        return False


# ─── Public API ──────────────────────────────────────────────────────────────

def scan_press_for_brands(brand_names: list[str]) -> dict[str, list[dict]]:
    """
    Scan all PRESS_FEEDS and return articles that mention any of the given brands.

    Args:
        brand_names: list of UPPERCASE brand names (as stored in DB)

    Returns:
        { BRAND_NAME_UPPER: [ {pub, vertical, title, url, date, snippet}, ... ] }
    """
    mentions: dict[str, list[dict]] = {}
    checked_at = datetime.now(timezone.utc).isoformat()

    for pub_name, feed_url, vertical in PRESS_FEEDS:
        try:
            articles = _fetch_feed(feed_url)
        except Exception as exc:
            logger.warning("Press feed error (%s): %s", pub_name, exc)
            continue

        if not articles:
            logger.debug("No articles from %s (%s)", pub_name, feed_url)
            continue

        logger.debug("Checking %d articles from %s", len(articles), pub_name)

        for article in articles:
            haystack = f"{article['title']} {article['summary']}"
            for brand in brand_names:
                if _matches(brand, haystack):
                    entry = {
                        "pub":       pub_name,
                        "vertical":  vertical,
                        "title":     article["title"],
                        "url":       article["url"],
                        "date":      article["date"],
                        "snippet":   article["summary"][:300],
                        "found_at":  checked_at,
                    }
                    mentions.setdefault(brand, []).append(entry)

    total = sum(len(v) for v in mentions.values())
    logger.info(
        "Press monitor: scanned %d feeds, found %d mentions across %d brands",
        len(PRESS_FEEDS), total, len(mentions),
    )
    return mentions
