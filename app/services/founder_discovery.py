"""
Founder Discovery Service

Given a brand name + optional filer name, attempts to identify the human founder
using a cascade of strategies:

1. If filer_name already looks like a person → return directly with confidence="high"
2. SerpAPI web search (brand + "founder" keywords) → parse with Claude Haiku
3. SerpAPI Product Hunt search → parse with Claude Haiku
4. Return None if no confident match found
"""
import json
import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

# Legal entity suffixes that indicate the name is NOT a person
_ENTITY_SUFFIXES = re.compile(
    r'\b(LLC|INC|CORP|LTD|LIMITED|LP|LLP|CO|COMPANY|INCORPORATED|'
    r'CORPORATION|HOLDINGS|GROUP|BRANDS|VENTURES|STUDIO|STUDIOS|'
    r'ENTERPRISES|PARTNERS|ASSOCIATES|TRUST|FUND|CAPITAL|LABS|'
    r'TECHNOLOGIES|TECH|SOLUTIONS|SERVICES|INTERNATIONAL|GLOBAL)\b',
    re.IGNORECASE,
)

# A person name looks like 2–4 capitalized words, all alphabetical (allowing hyphens/apostrophes)
_PERSON_NAME_RE = re.compile(
    r"^[A-Z][a-zA-Z'\-]+"
    r"(\s+[A-Z][a-zA-Z'\-]+){1,3}$"
)

_SERP_API_URL = "https://serpapi.com/search"
_SERP_TIMEOUT = 10  # seconds
_HAIKU_MODEL  = "claude-haiku-4-5"

_EXTRACTION_SYSTEM = (
    "You are a data extraction assistant. Given search result snippets about a brand, "
    "extract the most likely founder/CEO name. Return ONLY valid JSON: "
    '{"name": "<full name or null>", "confidence": "high|medium|low", "source_hint": "<brief reason>"}'
    " — no markdown, no explanation."
)


def looks_like_person(name: str) -> bool:
    """
    Return True if `name` appears to be a human name rather than a legal entity.

    Heuristic:
    - Must be 2–4 space-separated tokens
    - Each token starts with a capital letter
    - Contains no known legal-entity suffixes (LLC, INC, etc.)
    - Contains no digits
    """
    if not name or not name.strip():
        return False

    stripped = name.strip()

    # Reject if it contains digits
    if re.search(r'\d', stripped):
        return False

    # Reject if it contains a known entity suffix
    if _ENTITY_SUFFIXES.search(stripped):
        return False

    # Must match the person-name pattern
    return bool(_PERSON_NAME_RE.match(stripped))


def _serp_search(query: str) -> list:
    """
    Call SerpAPI and return up to 5 organic result dicts (title + snippet).
    Returns an empty list if the API key is missing or the request fails.
    """
    api_key = os.environ.get("SERPAPI_API_KEY", "").strip()
    if not api_key:
        logger.warning("SERPAPI_API_KEY not set — skipping web search")
        return []

    try:
        resp = requests.get(
            _SERP_API_URL,
            params={"q": query, "api_key": api_key, "num": 5, "engine": "google"},
            timeout=_SERP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("organic_results", [])
        return [
            {"title": r.get("title", ""), "snippet": r.get("snippet", "")}
            for r in results[:5]
        ]
    except requests.exceptions.RequestException as exc:
        logger.warning("SerpAPI request failed: %s", exc)
        return []
    except (ValueError, KeyError) as exc:
        logger.warning("SerpAPI response parse error: %s", exc)
        return []


def _extract_founder_from_snippets(brand_name: str, snippets: list) -> dict:
    """
    Pass search snippets to Claude Haiku and ask it to extract the founder name.
    Returns {"name": str|None, "confidence": str, "source_hint": str}.
    """
    if not snippets:
        return {"name": None, "confidence": "low", "source_hint": "no snippets"}

    from ..services.enrichment import _get_client  # reuse the existing Anthropic client

    try:
        client = _get_client()
    except RuntimeError as exc:
        logger.warning("Anthropic client unavailable for founder extraction: %s", exc)
        return {"name": None, "confidence": "low", "source_hint": "api_unavailable"}

    snippet_text = "\n".join(
        f"[{i+1}] {s['title']} — {s['snippet']}"
        for i, s in enumerate(snippets)
    )
    user_message = (
        f"Brand: {brand_name}\n\n"
        f"Search results:\n{snippet_text}\n\n"
        f"Who is the founder or CEO of {brand_name}? "
        f"Return JSON with name (or null if not found), confidence, and source_hint."
    )

    try:
        message = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=200,
            system=_EXTRACTION_SYSTEM,
            messages=[{"role": "user", "content": user_message}],
            timeout=20,
        )
        text = message.content[0].text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Haiku founder extraction JSON parse error: %s", exc)
        return {"name": None, "confidence": "low", "source_hint": "parse_error"}
    except Exception as exc:
        logger.warning("Haiku founder extraction failed: %s", exc)
        return {"name": None, "confidence": "low", "source_hint": "api_error"}


def discover_founder(
    brand_name: str,
    filer_name: str = None,
    category: str = None,
) -> dict:
    """
    Attempt to identify the human founder for a brand.

    Strategy (in order):
    1. If filer_name looks like a person → return directly with confidence="high"
    2. SerpAPI web search: `"<brand>" founder OR "founded by" OR CEO`
    3. SerpAPI Product Hunt search: `site:producthunt.com "<brand>"`
    4. Return name=None if nothing found

    Returns:
        {
            "name":       str | None,
            "confidence": "high" | "medium" | "low",
            "source":     str
        }
    """
    # ── 1. Filer name heuristic ───────────────────────────────────────────────
    if filer_name and looks_like_person(filer_name):
        logger.info("Founder discovery: filer_name '%s' looks like a person (high confidence)", filer_name)
        return {"name": filer_name, "confidence": "high", "source": "filer_name"}

    # ── 2. Web search ─────────────────────────────────────────────────────────
    web_query = f'"{brand_name}" founder OR "founded by" OR CEO'
    web_snippets = _serp_search(web_query)
    if web_snippets:
        result = _extract_founder_from_snippets(brand_name, web_snippets)
        if result.get("name") and result.get("confidence") in ("high", "medium"):
            logger.info(
                "Founder discovery: found '%s' via web search (confidence=%s)",
                result["name"], result["confidence"],
            )
            return {
                "name":       result["name"],
                "confidence": result["confidence"],
                "source":     "web_search",
            }

    # ── 3. Product Hunt search ────────────────────────────────────────────────
    ph_query = f'site:producthunt.com "{brand_name}"'
    ph_snippets = _serp_search(ph_query)
    if ph_snippets:
        result = _extract_founder_from_snippets(brand_name, ph_snippets)
        if result.get("name") and result.get("confidence") in ("high", "medium"):
            logger.info(
                "Founder discovery: found '%s' via Product Hunt search (confidence=%s)",
                result["name"], result["confidence"],
            )
            return {
                "name":       result["name"],
                "confidence": result["confidence"],
                "source":     "producthunt_search",
            }

    # ── 4. Nothing found ──────────────────────────────────────────────────────
    logger.info("Founder discovery: no confident match found for '%s'", brand_name)
    return {"name": None, "confidence": "low", "source": "not_found"}
