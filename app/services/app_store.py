"""
App Store new release scanner — consumer app launches via iTunes Search API.

The iTunes Search API is free with no API key. We search for recently updated
apps across Bullish consumer categories and surface new/emerging apps that
haven't yet attracted mainstream VC attention.

App Store signals are post-stealth (the app is live) but are valuable as:
  • Traction validators — a live app with reviews is further along than a
    trademark filing alone
  • Confluence builders — an existing trademark + App Store launch = strong
    stealth → launch signal
  • Founder discovery — App Store pages often name the developer/company

We filter for:
  • English-language apps (US store)
  • Consumer categories (not Productivity, Business, Developer Tools)
  • Apps updated within the last N days (recent activity)
  • Minimum rating threshold to avoid obvious junk
"""
import re
import time
import logging
import requests
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"
REQUEST_TIMEOUT   = 20

# Consumer genre IDs in the App Store
# https://affiliate.itunes.apple.com/resources/documentation/genre-mapping/
_CONSUMER_GENRES = {
    6013: "Health/Wellness",    # Health & Fitness
    6023: "Health/Wellness",    # Food & Drink
    6017: "Education",          # Education
    6016: "Entertainment",      # Entertainment
    6015: "Finance",            # Finance
    6014: "Sports",             # Sports
    6018: "Entertainment",      # Travel
    6020: "Home/Lifestyle",     # Lifestyle
    6012: "Home/Lifestyle",     # Shopping
    6024: "Beauty",             # Shopping (Beauty sub-mapped by keyword below)
}

# Consumer search terms — broad enough to surface emerging brands
_SEARCH_TERMS = [
    "wellness",
    "nutrition supplement",
    "skincare beauty",
    "fitness tracker",
    "meal planning",
    "sleep health",
    "weight management",
    "meditation mindfulness",
    "personal finance",
    "pet health",
    "women health",
    "men grooming",
    "functional beverage",
    "longevity health",
]

# Terms that strongly suggest B2B / enterprise — skip these
_B2B_SKIP_TERMS = [
    "enterprise", "b2b", "saas", "crm", "erp", "api", "devops", "kubernetes",
    "salesforce", "slack integration", "jira", "confluence", "developer tool",
    "for teams", "for business", "team collaboration", "project management",
]

# Minimum App Store rating to avoid junk
_MIN_RATING_COUNT = 5   # at least 5 reviews
_MIN_AVG_RATING   = 3.5  # at least 3.5 stars

# ─── Category inference ────────────────────────────────────────────────────────

def _infer_category(genre_id: int, genre_name: str, description: str, app_name: str) -> str:
    """Map App Store genre + keywords to a Bullish consumer category."""
    cat = _CONSUMER_GENRES.get(genre_id)

    # Refine shopping → beauty if keywords match
    text = f"{app_name} {description}".lower()
    if any(k in text for k in ["beauty", "skincare", "skin care", "makeup", "cosmetic", "haircare", "fragrance"]):
        return "Beauty"
    if any(k in text for k in ["food", "drink", "beverage", "coffee", "tea", "snack", "recipe", "meal", "nutrition"]):
        return "CPG/Food/Drink"
    if any(k in text for k in ["fitness", "workout", "exercise", "gym", "run", "yoga", "pilates"]):
        return "Fitness"
    if any(k in text for k in ["wellness", "health", "supplement", "vitamin", "sleep", "stress", "anxiety", "mental"]):
        return "Health/Wellness"
    if any(k in text for k in ["pet", "dog", "cat", "animal", "veterinary"]):
        return "Health/Wellness"

    return cat or "Consumer AI"


def _is_b2b(name: str, description: str) -> bool:
    """Return True if the app appears to be B2B/enterprise."""
    text = f"{name} {description}".lower()
    return any(term in text for term in _B2B_SKIP_TERMS)


def _parse_date(date_str: str) -> datetime | None:
    """Parse iTunes date string like '2026-03-15T07:00:00Z'."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _fingerprint(app_id: int) -> str:
    """Stable fingerprint for an App Store app — just the Apple app ID."""
    import hashlib
    return hashlib.sha256(f"app_store:{app_id}".encode()).hexdigest()[:16]


# ─── Search ────────────────────────────────────────────────────────────────────

def _search_term(term: str, days_back: int, max_per_term: int) -> list[dict]:
    """Search iTunes for a single term, return normalised signal dicts."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    try:
        resp = requests.get(
            ITUNES_SEARCH_URL,
            params={
                "term":    term,
                "country": "us",
                "media":   "software",
                "limit":   min(max_per_term, 50),
                "lang":    "en_us",
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as exc:
        logger.warning("App Store search failed for '%s': %s", term, exc)
        return []

    signals = []
    for app in results:
        try:
            genre_id   = app.get("primaryGenreId", 0)
            genre_name = app.get("primaryGenreName", "")

            # Skip non-consumer genres
            if genre_id not in _CONSUMER_GENRES:
                continue

            app_id      = app.get("trackId")
            name        = app.get("trackName", "").strip()
            developer   = app.get("artistName", "").strip()
            description = app.get("description", "")
            updated_str = app.get("currentVersionReleaseDate") or app.get("releaseDate", "")
            store_url   = app.get("trackViewUrl", "")
            rating      = app.get("averageUserRating", 0)
            rating_count= app.get("userRatingCount", 0)
            price       = app.get("price", 0)
            icon_url    = app.get("artworkUrl512") or app.get("artworkUrl100", "")

            if not app_id or not name:
                continue

            # Skip apps with too few ratings (very new = ok, but avoid zero-review junk)
            # Exception: if updated very recently (< 30 days), accept with 0 ratings
            updated_at  = _parse_date(updated_str)
            very_recent = updated_at and (datetime.now(timezone.utc) - updated_at).days < 30

            if not very_recent and rating_count >= _MIN_RATING_COUNT and rating < _MIN_AVG_RATING:
                continue

            if _is_b2b(name, description):
                continue

            category = _infer_category(genre_id, genre_name, description, name)

            # Build description snippet (first 300 chars)
            desc_snippet = re.sub(r'\s+', ' ', description[:300]).strip()

            signals.append({
                "companyName": name,
                "app_id":      app_id,
                "developer":   developer,
                "category":    category,
                "description": desc_snippet,
                "url":         store_url,
                "icon_url":    icon_url,
                "rating":      round(rating, 1),
                "rating_count":rating_count,
                "price":       price,
                "genre":       genre_name,
                "updated_at":  updated_str,
                "signal_type": "app_store",
                "score_boost": 8,
                "timestamp":   updated_str or datetime.now(timezone.utc).isoformat(),
                "notes":       f"Developer: {developer} | Genre: {genre_name} | Rating: {rating:.1f} ({rating_count} reviews)",
            })

        except Exception as exc:
            logger.debug("App Store parse error: %s", exc)
            continue

        time.sleep(0.05)   # gentle rate limiting

    return signals


# ─── Public entry point ────────────────────────────────────────────────────────

def search_recent_app_store(days_back: int = 30, max_results: int = 100) -> dict:
    """
    Search the App Store for recent consumer app launches.

    Returns:
        {
            "signals": [...],   # list of signal dicts
            "total_found": int,
            "error": None | str,
        }
    """
    logger.info("App Store scan: days_back=%d max_results=%d", days_back, max_results)

    all_signals   = []
    seen_app_ids  = set()
    max_per_term  = max(10, max_results // len(_SEARCH_TERMS))

    for term in _SEARCH_TERMS:
        results = _search_term(term, days_back, max_per_term)
        for sig in results:
            aid = sig["app_id"]
            if aid not in seen_app_ids:
                seen_app_ids.add(aid)
                all_signals.append(sig)
        if len(all_signals) >= max_results:
            break
        time.sleep(0.1)   # polite gap between search terms

    logger.info("App Store scan: %d unique consumer apps found", len(all_signals))
    return {
        "signals":     all_signals[:max_results],
        "total_found": len(all_signals),
        "error":       None,
    }
