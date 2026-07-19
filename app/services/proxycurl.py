"""
LinkedIn profile enrichment via FreshData (RapidAPI) — by-URL, identity-pre-resolved.

Replaces NinjaPear/Nubela (shut down July 2025, LinkedIn lawsuit) with a by-URL
LinkedIn scraper. Identity is pre-resolved: every watchlist person carries a
linkedin_url in data/watchlist_seed.csv, so we never pay to resolve a name → URL.

Cost: ~$0.0025/profile (FreshData Basic, $25/mo for 10k calls).
At ~200 watchlist people/month + ~10-40 founder enrichments: ~$0.50-$0.60/month.

Provider: Fresh LinkedIn Profile Data — https://rapidapi.com/freshdata-freshdata-default/api/fresh-linkedin-profile-data
Endpoint: GET https://fresh-linkedin-profile-data.p.rapidapi.com/get-linkedin-profile
Auth: X-RapidAPI-Key header (env var: PROXYCURL_API_KEY — reused from prior provider).

Only fires when:
  - PROXYCURL_API_KEY is set in environment
  - A linkedin_url is already known (watchlist seed or discovered hint)
  Never pays to resolve an unknown name → URL.
"""

import os
import logging

import requests

logger = logging.getLogger(__name__)

_BASE    = "https://fresh-linkedin-profile-data.p.rapidapi.com"
_HOST    = "fresh-linkedin-profile-data.p.rapidapi.com"
_TIMEOUT = 30


def _api_key() -> str | None:
    return os.environ.get("PROXYCURL_API_KEY")


def _headers() -> dict:
    return {
        "X-RapidAPI-Key":  _api_key() or "",
        "X-RapidAPI-Host": _HOST,
    }


def _fetch_by_url(linkedin_url: str) -> dict | None:
    """
    Fetch a LinkedIn profile by URL from FreshData.
    Returns raw API response dict or None on any failure.
    """
    key = _api_key()
    if not key or not linkedin_url:
        return None

    try:
        resp = requests.get(
            f"{_BASE}/get-linkedin-profile",
            params={"linkedin_url": linkedin_url, "include_skills": "false"},
            headers=_headers(),
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("FreshData profile request failed for %s: %s", linkedin_url, exc)
        return None

    if resp.status_code == 429:
        logger.warning("FreshData: rate limit hit for %s", linkedin_url)
        return None
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        logger.warning(
            "FreshData profile %s → HTTP %d: %s",
            linkedin_url, resp.status_code, resp.text[:200],
        )
        return None

    return resp.json()


# ── Public interface ──────────────────────────────────────────────────────────

def should_enrich_founder(enrichment: dict) -> bool:
    """Return True if this enrichment result qualifies for a LinkedIn profile lookup."""
    if not _api_key():
        return False
    score   = enrichment.get("bullish_score") or 0
    founder = enrichment.get("founder") or {}
    if score < 50:
        return False
    # Requires a pre-resolved URL — never pays for name-based search
    if not founder.get("linkedin_url"):
        return False
    return True


def build_context(profile: dict) -> dict:
    """
    Map a raw FreshData profile dict to the standard context dict
    consumed by rescore_founder_with_linkedin.
    """
    experiences = []
    for exp in (profile.get("experiences") or [])[:5]:
        start_obj = exp.get("starts_at") or {}
        end_obj   = exp.get("ends_at")   or {}
        experiences.append({
            "company": exp.get("company"),
            "title":   exp.get("title"),
            "start":   str(start_obj["year"]) if start_obj.get("year") else None,
            "end":     str(end_obj["year"]) if end_obj.get("year") else "present",
        })

    education = []
    for edu in (profile.get("education") or [])[:3]:
        education.append({
            "school": edu.get("school"),
            "degree": edu.get("degree_name"),
            "field":  edu.get("field_of_study"),
        })

    return {
        "linkedin_url":    profile.get("profile_url") or "",
        "headline":        profile.get("headline") or profile.get("occupation") or "",
        "summary":         (profile.get("about") or "")[:600],
        "follower_count":  profile.get("follower_count"),
        "connections":     profile.get("connections"),
        "experiences":     experiences,
        "education":       education,
        "recommendations": 0,
    }


def enrich_founder(founder_name: str, brand_name: str, linkedin_url: str = None) -> dict:
    """
    Fetch LinkedIn profile for a founder — by URL only.

    When no linkedin_url is provided, returns {"found": False} without any
    network call. The caller should pass linkedin_url from watchlist_seed.csv
    or a previously discovered hint; never pay to resolve an unknown URL.
    """
    empty = {"found": False}

    if not linkedin_url:
        logger.info(
            "LinkedIn enrichment: no URL known for %s / %s — skipping",
            founder_name, brand_name,
        )
        return empty

    profile = _fetch_by_url(linkedin_url)
    if not profile:
        logger.info(
            "LinkedIn enrichment: no profile returned for %s (%s)",
            founder_name, linkedin_url,
        )
        return empty

    ctx          = build_context(profile)
    ctx["found"] = True
    logger.info(
        "LinkedIn enrichment: enriched %s (%s) — %d experiences",
        founder_name, linkedin_url, len(ctx.get("experiences", [])),
    )
    return ctx


def fetch_linkedin_profile(linkedin_url: str) -> dict | None:
    """
    Fetch a profile by URL and return the full context dict.
    Used by founder_enrichment.py when a linkedin_url_hint is available.
    Returns None on failure.
    """
    if not linkedin_url:
        return None
    profile = _fetch_by_url(linkedin_url)
    if not profile:
        return None
    ctx = build_context(profile)
    ctx["found"] = True
    return ctx


def fetch_linkedin_headline(linkedin_url: str) -> dict | None:
    """
    Fetch just the current headline for a person by their LinkedIn URL.
    Used by the monthly watchlist sweep in scheduler.py.
    Returns {"headline": str, "full_name": str} or None on any failure.
    """
    if not linkedin_url:
        return None
    profile = _fetch_by_url(linkedin_url)
    if not profile:
        return None
    return {
        "headline":  profile.get("headline") or profile.get("occupation") or "",
        "full_name": profile.get("full_name") or "",
    }


# ── Stealth-shift detection ───────────────────────────────────────────────────

_STEALTH_KEYWORDS = frozenset([
    "founder", "co-founder", "cofounder", "ceo", "chief executive",
    "building", "stealth", "new venture", "entrepreneur",
    "starting", "launching", "operator",
])


def is_stealth_headline(headline: str | None) -> bool:
    """Return True if a LinkedIn headline suggests the person is founding / going stealth."""
    if not headline:
        return False
    h = headline.lower()
    return any(kw in h for kw in _STEALTH_KEYWORDS)


# ── Compatibility shims ───────────────────────────────────────────────────────
# NinjaPear had name-based search; FreshData requires a URL.
# These return None so callers fall through to their existing no-result paths.

def search_person(founder_name: str, brand_name: str) -> str | None:
    return None


def get_profile(linkedin_url: str) -> dict | None:
    return fetch_linkedin_profile(linkedin_url)


def find_linkedin_url(name: str, brand_name: str = None) -> str | None:
    return None


def fetch_person_profile(name: str, brand: str) -> dict | None:
    return None
