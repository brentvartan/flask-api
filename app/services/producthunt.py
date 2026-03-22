"""
Product Hunt scanner — consumer product launches via RSS feed.

Product Hunt is a post-stealth signal: brands are already launching publicly.
But for Bullish it is still valuable as an early-traction validator and as a
source of consumer brands that just broke cover — often still pre-seed and
before institutional VCs have found them.

No API key required. Uses the public RSS feed.
"""
import re
import logging
import xml.etree.ElementTree as ET
import urllib.request
import urllib.error

from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

PH_RSS_URL      = "https://www.producthunt.com/feed"
REQUEST_TIMEOUT = 15

# ── Consumer keyword → category ───────────────────────────────────────────────

_CATEGORY_KEYWORDS = {
    "Beauty": [
        "beauty", "skincare", "skin care", "cosmetic", "cosmetics",
        "makeup", "hair care", "haircare", "glow", "serum", "lip",
        "nail", "fragrance", "sunscreen", "spf",
    ],
    "Health/Wellness": [
        "health", "wellness", "supplement", "vitamin", "nutrition",
        "gut health", "sleep", "stress", "anxiety", "mental health",
        "longevity", "biohack", "weight loss", "glp", "perimenopause",
        "hormone", "fertility", "probiotic", "microbiome",
    ],
    "CPG/Food/Drink": [
        "food", "drink", "beverage", "coffee", "tea", "snack",
        "protein", "meal kit", "recipe", "plant-based", "organic",
        "zero sugar", "no alcohol", "mocktail", "adaptogenic",
        "functional drink", "bar ", "bars", "chocolate",
    ],
    "Fitness": [
        "fitness", "workout", "exercise", "gym", "yoga", "running",
        "cycling", "strength training", "cardio", "sport", "athletic",
    ],
    "Apparel": [
        "clothing", "apparel", "fashion", "wear", "shirt", "dress",
        "shoes", "sneakers", "jacket", "activewear", "athleisure",
    ],
    "Home/Lifestyle": [
        "home decor", "interior", "furniture", "kitchen", "garden",
        "cleaning", "organization", "candle", "pet accessory",
    ],
    "Consumer AI": [
        "personalized", "personalization", "ai-powered", "ai coach",
        "smart device", "wearable",
    ],
    "Education": [
        "learn", "education", "course", "skill", "teach", "tutor",
    ],
    "Entertainment": [
        "entertainment", "game", "gaming", "music", "podcast", "creator",
    ],
    "Finance": [
        "fintech", "savings", "investing", "budget", "personal finance",
    ],
    "Pet": [
        "pet food", "dog food", "cat food", "pet health", "pet care",
        "veterinary", "dog treat", "cat treat",
    ],
}

# Keywords that strongly suggest B2B / developer / platform products to exclude
_B2B_SIGNALS = [
    "api ", "sdk", "saas", "b2b", "enterprise", "developer tool",
    "open source", "cli ", "self-hosted", "data pipeline", "crm ",
    "erp ", "analytics platform", "dashboard for teams",
]


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _infer_category(text: str) -> tuple[str, bool]:
    """
    Returns (category, is_consumer).
    is_consumer=False if the text looks like a B2B / developer product.
    """
    text_lower = text.lower()

    # Exclude obvious B2B / dev tools
    if any(kw in text_lower for kw in _B2B_SIGNALS):
        return "Other", False

    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return category, True

    # Default: Consumer AI (PH skews towards tech products)
    return "Consumer AI", True


def _parse_pub_date(date_str: str) -> datetime:
    try:
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.now(timezone.utc)


# ── Main service function ─────────────────────────────────────────────────────

def search_recent_producthunt(days_back: int = 14, max_results: int = 100) -> dict:
    """
    Fetch recent Product Hunt launches and return consumer brand signals.

    Returns:
        {
            "signals":     [...],
            "total_found": int,
            "fetched":     int,
            "error":       str | None,
        }
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    try:
        req = urllib.request.Request(
            PH_RSS_URL,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            },
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            rss_bytes = resp.read()
        rss_text = rss_bytes.decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return {"signals": [], "total_found": 0, "fetched": 0, "error": str(exc)}

    try:
        root = ET.fromstring(rss_text)
    except ET.ParseError as exc:
        return {"signals": [], "total_found": 0, "fetched": 0, "error": f"RSS parse error: {exc}"}

    raw_items = root.findall(".//item")
    total_found = len(raw_items)
    signals = []

    for item in raw_items[:max_results]:
        title     = _strip_html(item.findtext("title") or "").strip()
        desc      = _strip_html(item.findtext("description") or "").strip()
        link      = (item.findtext("link") or "").strip()
        pub_raw   = (item.findtext("pubDate") or "").strip()

        # Also check for category tags Product Hunt sometimes includes
        category_tags = [c.text for c in item.findall("category") if c.text]

        if not title:
            continue

        pub_dt = _parse_pub_date(pub_raw)
        if pub_dt < cutoff:
            continue

        combined = f"{title} {desc} {' '.join(category_tags)}"
        category, is_consumer = _infer_category(combined)

        if not is_consumer:
            continue

        # Truncate description
        tagline = desc[:200] if desc else title

        signals.append({
            "companyName":  title,
            "signal_type":  "producthunt",
            "category":     category,
            "score_boost":  8,
            "description":  f"{title} — Product Hunt launch",
            "url":          link,
            "notes":        tagline,
            "timestamp":    pub_dt.isoformat(),
        })

    return {
        "signals":     signals,
        "total_found": total_found,
        "fetched":     len(signals),
        "error":       None,
    }
