"""
Newswire scanner — consumer brand press releases via PR Newswire and BusinessWire RSS.

Targets seed-funded or newly-launched consumer brands that are officially out of stealth
but not yet widely covered. Complements trademark/EDGAR signals with brands a step
further along: funded, shipping, or just broke cover.

No API key required. Uses public RSS feeds.
"""
import re
import logging
import xml.etree.ElementTree as ET
import urllib.request
import urllib.error

from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20

# ── RSS feed sources ──────────────────────────────────────────────────────────

FEEDS = [
    ("prnewswire", "https://www.prnewswire.com/rss/news-releases-list.rss"),
    ("businesswire", "https://www.businesswire.com/rss/home/?rss=G7"),   # Consumer Products
    ("businesswire", "https://www.businesswire.com/rss/home/?rss=G22"),  # Food & Beverage
    ("businesswire", "https://www.businesswire.com/rss/home/?rss=G14"),  # Health
]

# ── Consumer brand categories → keywords ─────────────────────────────────────

_CATEGORY_KEYWORDS = {
    "Beauty": [
        "beauty", "skincare", "skin care", "cosmetic", "cosmetics",
        "makeup", "hair care", "haircare", "glow", "serum", "lip",
        "nail", "fragrance", "sunscreen", "spf", "derma", "aesthetic",
    ],
    "Health/Wellness": [
        "health", "wellness", "supplement", "vitamin", "nutrition",
        "gut health", "sleep", "stress", "mental health",
        "longevity", "biohack", "weight loss", "glp", "perimenopause",
        "hormone", "fertility", "probiotic", "microbiome", "functional",
        "recovery", "nootropic",
    ],
    "CPG/Food/Drink": [
        "food", "drink", "beverage", "coffee", "tea", "snack",
        "protein", "meal kit", "plant-based", "organic",
        "zero sugar", "no alcohol", "mocktail", "adaptogenic",
        "functional drink", "chocolate", "bar ", "bars", "sauce",
        "condiment", "cereal", "granola", "cuisine", "culinary",
    ],
    "Fitness": [
        "fitness", "workout", "exercise", "gym", "yoga", "running",
        "cycling", "strength training", "sport", "athletic", "recovery",
        "activewear", "training",
    ],
    "Apparel": [
        "clothing", "apparel", "fashion", "wear", "shirt", "dress",
        "shoes", "sneakers", "jacket", "athleisure", "denim",
        "outerwear", "streetwear",
    ],
    "Home/Lifestyle": [
        "home decor", "interior", "kitchen", "garden",
        "cleaning", "organization", "candle", "pet food", "pet care",
        "baby", "parenting", "nursery",
    ],
    "Consumer AI": [
        "personalized", "ai-powered", "ai coach", "wearable",
        "smart device", "consumer app", "personalization",
    ],
}

# Release must contain at least one of these to be relevant (funded or just launching)
_STAGE_KEYWORDS = [
    "seed", "pre-seed", "pre seed", "raises", "raised", "raise",
    "funding", "capital", "investment", "backed by", "closes round",
    "launch", "launches", "launched", "launching",
    "introduces", "introduced", "unveils", "unveiled",
    "debuts", "debuted", "new brand", "announces new",
]

# Hard excludes — filters out B2B, pharma, finance, and non-brand content
_EXCLUDE_KEYWORDS = [
    "acquisition", "acquires", "merger", "merges", "ipo", "going public",
    "earnings", "quarterly results", "dividend", "layoffs", "lawsuit", "settlement",
    "fda approval", "clinical trial", "phase ", "pharma", "biotech", "biopharmaceutical",
    "enterprise software", "b2b", "saas", "platform for businesses",
    "wholesale", "distributor", "manufacturing", "supply chain", "logistics",
    "real estate", "mortgage", "insurance", "banking", "fintech", "cryptocurrency",
    "media company", "production company", "law firm", "consulting",
]

# Verbs that typically follow the company name in a PR title
_VERB_PATTERN = re.compile(
    r'\b(announce[sd]?|raise[sd]?|launch(?:es|ed)?|introduce[sd]?|unveil[sd]?|'
    r'debut[sed]?|close[sd]?|partner[s]?|secure[sd]?|receives?|named?|'
    r'appoint[s]?|expand[sd]?|complete[sd]?|join[s]?|hire[sd]?)\b',
    re.IGNORECASE,
)


def _extract_company_name(title: str) -> str | None:
    """Extract the company name from the front of a PR title."""
    # Strip common news prefixes
    title = re.sub(
        r'^(EXCLUSIVE|BREAKING|UPDATE|NEW|CORRECTION|SPONSORED|ADVISORY):\s*',
        '', title, flags=re.IGNORECASE,
    ).strip()

    # Find the first verb that typically follows the company name
    match = _VERB_PATTERN.search(title)
    if match:
        candidate = title[:match.start()].strip().rstrip(',:').strip()
        if 2 <= len(candidate) <= 60:
            return candidate

    # Fallback: take words before first comma or em-dash
    short = re.split(r'[,—–]', title)[0].strip()
    words = short.split()
    candidate = ' '.join(words[:4]).strip()
    if 2 <= len(candidate) <= 60:
        return candidate

    return None


def _detect_category(text: str) -> str | None:
    """Return the best-matching consumer category from combined title+description text."""
    lower = text.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return category
    return None


def _fetch_feed(source: str, url: str) -> list[ET.Element]:
    """Fetch and parse one RSS feed. Returns a list of <item> elements."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; BullishSignalBot/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        raw = resp.read()

    root = ET.fromstring(raw)
    channel = root.find("channel")
    return channel.findall("item") if channel is not None else root.findall(".//item")


def search_recent_newswire(days_back: int = 30, max_results: int = 100) -> dict:
    """
    Scan PR Newswire and BusinessWire RSS feeds for recent consumer brand signals.

    Returns {"signals": [...], "error": None} on success,
    or {"signals": [], "error": "..."} if all feeds failed.
    """
    cutoff  = datetime.now(timezone.utc) - timedelta(days=days_back)
    signals = []
    errors  = []

    for source, feed_url in FEEDS:
        try:
            items = _fetch_feed(source, feed_url)
        except (urllib.error.URLError, ET.ParseError, Exception) as exc:
            logger.warning("Newswire feed failed (%s): %s", feed_url, exc)
            errors.append(f"{source}: {exc}")
            continue

        for item in items:
            if len(signals) >= max_results:
                break

            title_el   = item.find("title")
            link_el    = item.find("link")
            desc_el    = item.find("description")
            pubdate_el = item.find("pubDate")

            title    = (title_el.text    or "").strip() if title_el    is not None else ""
            link     = (link_el.text     or "").strip() if link_el     is not None else ""
            raw_desc = (desc_el.text     or "").strip() if desc_el     is not None else ""
            pub_str  = (pubdate_el.text  or "").strip() if pubdate_el  is not None else ""

            if not title:
                continue

            # Parse publish date
            try:
                pub_dt = parsedate_to_datetime(pub_str)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            except Exception:
                pub_dt = datetime.now(timezone.utc)

            if pub_dt < cutoff:
                continue

            combined = f"{title} {raw_desc}".lower()

            # Hard excludes first
            if any(ex in combined for ex in _EXCLUDE_KEYWORDS):
                continue

            # Must be a funding or launch event
            if not any(kw in combined for kw in _STAGE_KEYWORDS):
                continue

            # Must match a consumer category
            category = _detect_category(combined)
            if not category:
                continue

            # Extract company name from title
            company_name = _extract_company_name(title)
            if not company_name:
                continue

            # Strip HTML from description
            clean_desc = re.sub(r'<[^>]+>', ' ', raw_desc)
            clean_desc = re.sub(r'\s+', ' ', clean_desc).strip()[:500]

            signals.append({
                "companyName":  company_name,
                "signal_type":  "newswire",
                "category":     category,
                "description":  clean_desc or title,
                "url":          link,
                "notes":        source,
                "timestamp":    pub_dt.isoformat(),
                "score_boost":  0,
            })

    if not signals and errors:
        return {"signals": [], "error": "; ".join(errors)}

    logger.info("Newswire scan: %d signals found across %d feeds", len(signals), len(FEEDS))
    return {"signals": signals, "error": None}
