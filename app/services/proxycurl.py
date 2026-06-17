"""
NinjaPear professional profile enrichment — WARM+ signals only.
(Replaces the now-sunset Proxycurl API; same API key, new endpoints at nubela.co/api/)

Only fires when ALL of these are true:
  - bullish_score >= 50  (WARM tier or above)
  - founder.name is not null
  - founder.confidence != 'unknown'
  - PROXYCURL_API_KEY is set in environment

Cost: 3 credits per profile = ~$0.04/founder
At ~10 WARM signals/week: ~$1.60/month

Flow: single GET /api/v2/employee/profile with first_name + last_name + employer_website
"""

import os
import logging

import requests

logger = logging.getLogger(__name__)

_BASE    = "https://nubela.co"
_TIMEOUT = 40   # NinjaPear averages 10.5s (fast) to 38.7s (detailed)


def _api_key() -> str | None:
    return os.environ.get("PROXYCURL_API_KEY")


def should_enrich_founder(enrichment: dict) -> bool:
    """Return True if this enrichment result qualifies for a profile lookup."""
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


def _fetch_profile(founder_name: str, brand_name: str) -> dict | None:
    """
    Call NinjaPear /api/v2/employee/profile with name + employer.
    Returns the raw API response dict or None on failure.
    """
    key = _api_key()
    if not key:
        return None

    parts      = founder_name.strip().split()
    first_name = parts[0] if parts else ""
    last_name  = " ".join(parts[1:]) if len(parts) > 1 else ""

    try:
        resp = requests.get(
            f"{_BASE}/api/v2/employee/profile",
            params={
                "first_name":       first_name,
                "last_name":        last_name,
                "employer_website": brand_name,   # accepts company name or website URL
            },
            headers={"Authorization": f"Bearer {key}"},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("NinjaPear profile request failed for %s: %s", founder_name, exc)
        return None

    if resp.status_code == 402:
        logger.warning("NinjaPear: insufficient credits")
        return None
    if resp.status_code == 404:
        return None   # not found — normal, not an error
    if resp.status_code == 400:
        # NinjaPear returns 400 when it can't resolve a company name to a website URL.
        # Brand names passed as employer_website (e.g. "Poppi") may fail; website URLs work best.
        logger.info("NinjaPear: could not resolve company for %s / %s (400)", founder_name, brand_name)
        return None
    if resp.status_code != 200:
        logger.warning("NinjaPear profile %s → HTTP %d: %s", founder_name, resp.status_code, resp.text[:200])
        return None

    return resp.json()


def search_person(founder_name: str, brand_name: str) -> str | None:
    """
    NinjaPear has no separate search step and doesn't return LinkedIn URLs.
    Compatibility shim — always returns None so callers fall through to enrich_founder.
    """
    return None


def get_profile(linkedin_url: str) -> dict | None:
    """
    Legacy interface. NinjaPear doesn't accept LinkedIn URLs as lookup keys.
    Returns None — founder_enrichment.py falls back to enrich_founder(name, brand).
    """
    return None


def build_context(profile: dict) -> dict:
    """
    Extract the fields Claude needs for founder re-scoring from a raw
    NinjaPear profile dict.  Keeps it small — only what maps to the
    5-signal scoring model.
    """
    # Work history: last 5 roles, most recent first
    experiences = []
    for exp in (profile.get("work_experience") or [])[:5]:
        start = (exp.get("start_date") or "")[:4]   # "YYYY-MM" → "YYYY"
        end   = (exp.get("end_date")   or "")[:4]
        experiences.append({
            "company": exp.get("company_name"),
            "title":   exp.get("role"),
            "start":   start or None,
            "end":     end or "present",
        })

    # Education: last 3 institutions
    education = []
    for edu in (profile.get("education") or [])[:3]:
        education.append({
            "school": edu.get("school"),
            "degree": edu.get("major"),   # NinjaPear folds degree+field into 'major'
            "field":  None,
        })

    # NinjaPear aggregates from X/Twitter; no LinkedIn URL is returned
    slug = profile.get("slug")
    linkedin_url = f"https://nubela.co/people/{slug}" if slug else None

    return {
        "linkedin_url":    linkedin_url,
        "headline":        profile.get("bio"),
        "summary":         (profile.get("bio") or "")[:600],
        "follower_count":  profile.get("follower_count"),
        "connections":     None,   # NinjaPear does not return LinkedIn connection count
        "experiences":     experiences,
        "education":       education,
        "recommendations": 0,
    }


def enrich_founder(founder_name: str, brand_name: str) -> dict:
    """
    Full NinjaPear flow: single profile fetch → structured context dict.

    Returns a context dict (possibly with found=False if nothing was found).
    The caller passes this to the Claude founder re-score function.
    """
    empty = {"found": False}

    profile = _fetch_profile(founder_name, brand_name)
    if not profile:
        logger.info("NinjaPear: no profile found for %s / %s", founder_name, brand_name)
        return empty

    ctx          = build_context(profile)
    ctx["found"] = True
    logger.info(
        "NinjaPear: enriched %s (%s) — %d experiences, %d education",
        founder_name, brand_name,
        len(ctx.get("experiences", [])),
        len(ctx.get("education", [])),
    )
    return ctx


# ── Compatibility aliases used by founder_enrichment.py ───────────────────────

def find_linkedin_url(name: str, brand_name: str = None) -> str | None:
    """
    NinjaPear doesn't expose LinkedIn URLs. Returns None.
    founder_enrichment.py falls back to enrich_founder(name, brand_name).
    """
    return None


def fetch_linkedin_profile(linkedin_url: str) -> dict | None:
    """
    NinjaPear doesn't accept LinkedIn URLs as input. Returns None.
    founder_enrichment.py has a fallback path that calls enrich_founder instead.
    """
    return None
