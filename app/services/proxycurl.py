"""
Proxycurl LinkedIn enrichment — WARM+ signals only.

Only fires when ALL of these are true:
  - bullish_score >= 50  (WARM tier or above)
  - founder.name is not null
  - founder.confidence != 'unknown'
  - PROXYCURL_API_KEY is set in environment

Cost: ~3 credits to search + 1 credit to fetch profile = ~$0.04/founder
At ~10 WARM signals/week: ~$1.60/month

Flow:
  1. search_person(name, brand)  → finds LinkedIn profile URL
  2. get_profile(url)            → fetches full work/education history
  3. build_context(profile)      → formats for Claude re-score prompt
"""

import os
import logging

import requests

logger = logging.getLogger(__name__)

_BASE      = "https://nubela.co/proxycurl/api"
_TIMEOUT   = 20


def _api_key() -> str | None:
    return os.environ.get("PROXYCURL_API_KEY")


def should_enrich_founder(enrichment: dict) -> bool:
    """Return True if this enrichment result qualifies for a LinkedIn lookup."""
    if not _api_key():
        return False

    score   = enrichment.get("bullish_score") or 0
    founder = enrichment.get("founder") or {}

    if score < 50:
        return False
    if not founder.get("name"):
        return False
    if founder.get("confidence") == "unknown":
        return False

    return True


def search_person(founder_name: str, brand_name: str) -> str | None:
    """
    Search Proxycurl for a founder's LinkedIn URL.
    Returns profile URL string or None if not found / API unavailable.
    """
    key = _api_key()
    if not key:
        return None

    parts      = founder_name.strip().split()
    first_name = parts[0] if parts else ""
    last_name  = " ".join(parts[1:]) if len(parts) > 1 else ""

    try:
        resp = requests.get(
            f"{_BASE}/search/person",
            params={
                "first_name":    first_name,
                "last_name":     last_name,
                "company_name":  brand_name,
                "page_size":     1,
                "enrich_profile": "skip",   # save credits — we fetch separately
            },
            headers={"Authorization": f"Bearer {key}"},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Proxycurl search request failed for %s: %s", founder_name, exc)
        return None

    if resp.status_code == 402:
        logger.warning("Proxycurl: insufficient credits")
        return None
    if resp.status_code != 200:
        logger.warning("Proxycurl search %s → HTTP %d", founder_name, resp.status_code)
        return None

    results = resp.json().get("results") or []
    if not results:
        return None

    return results[0].get("linkedin_profile_url")


def get_profile(linkedin_url: str) -> dict | None:
    """
    Fetch a full LinkedIn profile by URL.
    Returns the Proxycurl profile dict or None on failure.
    """
    key = _api_key()
    if not key or not linkedin_url:
        return None

    try:
        resp = requests.get(
            f"{_BASE}/v2/linkedin",
            params={"url": linkedin_url, "use_cache": "if-present"},
            headers={"Authorization": f"Bearer {key}"},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Proxycurl profile fetch failed for %s: %s", linkedin_url, exc)
        return None

    if resp.status_code != 200:
        logger.warning("Proxycurl profile %s → HTTP %d", linkedin_url, resp.status_code)
        return None

    return resp.json()


def build_context(profile: dict) -> dict:
    """
    Extract the fields Claude needs for founder re-scoring from a raw
    Proxycurl profile dict.  Keeps it small — only what maps to the
    5-signal scoring model.
    """
    # Work history: last 5 roles, most recent first
    experiences = []
    for exp in (profile.get("experiences") or [])[:5]:
        start = (exp.get("starts_at") or {}).get("year")
        end   = (exp.get("ends_at")   or {}).get("year")
        experiences.append({
            "company":  exp.get("company"),
            "title":    exp.get("title"),
            "start":    start,
            "end":      end or "present",
        })

    # Education: last 3 institutions
    education = []
    for edu in (profile.get("education") or [])[:3]:
        education.append({
            "school": edu.get("school"),
            "degree": edu.get("degree_name"),
            "field":  edu.get("field_of_study"),
        })

    return {
        "linkedin_url":    profile.get("public_identifier")
                           and f"https://linkedin.com/in/{profile['public_identifier']}",
        "headline":        profile.get("headline"),
        "summary":         (profile.get("summary") or "")[:600],   # cap for token budget
        "follower_count":  profile.get("follower_count"),
        "connections":     profile.get("connections"),
        "experiences":     experiences,
        "education":       education,
        "recommendations": len(profile.get("recommendations") or []),
    }


def enrich_founder(founder_name: str, brand_name: str) -> dict:
    """
    Full Proxycurl flow: search → profile → structured context dict.

    Returns a context dict (possibly with found=False if nothing was found).
    The caller passes this to the Claude founder re-score function.
    """
    empty = {"found": False}

    linkedin_url = search_person(founder_name, brand_name)
    if not linkedin_url:
        logger.info("Proxycurl: no LinkedIn match for %s / %s", founder_name, brand_name)
        return empty

    profile = get_profile(linkedin_url)
    if not profile:
        return empty

    ctx          = build_context(profile)
    ctx["found"] = True
    logger.info(
        "Proxycurl: enriched %s (%s) — %d experiences, %d education",
        founder_name, brand_name,
        len(ctx.get("experiences", [])),
        len(ctx.get("education", [])),
    )
    return ctx
