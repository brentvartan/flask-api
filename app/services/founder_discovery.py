"""
Founder Discovery Service

Given a brand name + optional filer name, attempts to identify the human founder
using a cascade of strategies:

1. If filer_name already looks like a person → return directly with confidence="high"
2. SerpAPI web search (brand + "founder" keywords) → parse with Claude Haiku
3. SerpAPI Product Hunt search → parse with Claude Haiku
4. Website scraping: find brand site → scrape About/Team page → extract founder via Claude Haiku
5. Return None if no confident match found

After name is determined, also runs exit background search.
"""
import json
import logging
import os
import re

import requests
from urllib.parse import urljoin, urlparse

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

# Domains to filter out when looking for official brand websites
_BLOCKED_DOMAINS = re.compile(
    r'(amazon\.com|etsy\.com|ebay\.com|walmart\.com|target\.com|'
    r'facebook\.com|instagram\.com|twitter\.com|tiktok\.com|youtube\.com|'
    r'linkedin\.com|pinterest\.com|reddit\.com|yelp\.com|'
    r'techcrunch\.com|forbes\.com|businesswire\.com|prnewswire\.com|'
    r'crunchbase\.com|angellist\.com|bloomberg\.com|wsj\.com|nytimes\.com|'
    r'producthunt\.com|g2\.com|capterra\.com)',
    re.IGNORECASE,
)

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
            {"title": r.get("title", ""), "snippet": r.get("snippet", ""), "link": r.get("link", "")}
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


def find_brand_website(brand_name: str, category: str, serpapi_key: str) -> str | None:
    """
    Uses SerpAPI to find the brand's official website.
    Filters out social media, marketplaces, news sites.
    Returns the homepage URL (string) or None.
    """
    if not serpapi_key:
        return None

    query = f'"{brand_name}" {category} official site'
    try:
        resp = requests.get(
            _SERP_API_URL,
            params={"q": query, "api_key": serpapi_key, "num": 5, "engine": "google"},
            timeout=_SERP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("organic_results", [])

        for result in results[:5]:
            link = result.get("link", "")
            if not link:
                continue
            parsed = urlparse(link)
            domain = parsed.netloc.lower()
            # Filter out blocked domains
            if _BLOCKED_DOMAINS.search(domain):
                continue
            # Return homepage (scheme + netloc only)
            homepage = f"{parsed.scheme}://{parsed.netloc}"
            logger.info("Found brand website for '%s': %s", brand_name, homepage)
            return homepage

    except requests.exceptions.RequestException as exc:
        logger.warning("SerpAPI brand website search failed for '%s': %s", brand_name, exc)
    except (ValueError, KeyError) as exc:
        logger.warning("SerpAPI brand website parse error for '%s': %s", brand_name, exc)

    return None


def scrape_about_page(website_url: str) -> str | None:
    """
    Fetches the brand website and looks for an About or Team page.
    Tries paths in order: /about, /team, /our-story, /founders, /about-us, homepage.
    Returns plain text (stripped of HTML tags) limited to 3000 chars, or None on any error.
    """
    if not website_url:
        return None

    paths_to_try = ["/about", "/team", "/our-story", "/founders", "/about-us", "/"]
    headers = {"User-Agent": "Mozilla/5.0"}

    for path in paths_to_try:
        url = urljoin(website_url, path)
        try:
            resp = requests.get(url, timeout=8, headers=headers)
            if resp.status_code == 200:
                html = resp.text
                # Strip HTML tags with simple regex
                text = re.sub(r'<[^>]+>', ' ', html)
                # Collapse whitespace
                text = re.sub(r'\s+', ' ', text).strip()
                if len(text) > 100:  # must have meaningful content
                    logger.info("Scraped %s (path=%s): %d chars", website_url, path, len(text))
                    return text[:3000]
        except Exception as exc:
            logger.debug("Scrape failed for %s%s: %s", website_url, path, exc)
            continue

    logger.info("No scrapeable page found at %s", website_url)
    return None


def extract_founders_from_page(page_text: str, brand_name: str, claude_client) -> list:
    """
    Passes scraped page text to Claude Haiku to extract founder info.
    Returns a list of dicts (may be empty):
      [{"name": str, "title": str, "bio_snippet": str, "linkedin_url": str|null}]
    """
    if not page_text or not claude_client:
        return []

    system_prompt = (
        "You are a data extraction assistant. Extract founder information from website text. "
        "Return ONLY valid JSON — no markdown, no explanation."
    )
    user_message = (
        f"From this About/Team page for {brand_name}, extract the founder(s) or CEO. "
        f'Return JSON: {{"founders": [{{"name": "str", "title": "str", "bio_snippet": "str", "linkedin_url": "str or null"}}]}} '
        f'If no founder found, return {{"founders": []}}.\n\n'
        f"Page text:\n{page_text[:2000]}"
    )

    try:
        message = claude_client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=400,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            timeout=20,
        )
        text = message.content[0].text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        parsed = json.loads(text)
        return parsed.get("founders", [])
    except json.JSONDecodeError as exc:
        logger.warning("Founder page extraction JSON parse error for '%s': %s", brand_name, exc)
        return []
    except Exception as exc:
        logger.warning("Founder page extraction failed for '%s': %s", brand_name, exc)
        return []


def search_exit_background(
    founder_name: str,
    brand_name: str,
    serpapi_key: str,
    claude_client,
) -> dict:
    """
    Searches for whether the founder worked at a brand that had a notable exit.
    Returns dict: {"has_exit_background": bool, "details": str|None}
    """
    if not serpapi_key or not founder_name:
        return {"has_exit_background": False, "details": None}

    all_snippets = []

    # Query 1: exit/acquisition signals
    exit_query = f'"{founder_name}" brand acquired OR "acquired by" OR exit OR sold'
    try:
        resp = requests.get(
            _SERP_API_URL,
            params={"q": exit_query, "api_key": serpapi_key, "num": 5, "engine": "google"},
            timeout=_SERP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("organic_results", [])
        for r in results[:5]:
            snippet = r.get("snippet", "")
            if snippet:
                all_snippets.append(snippet)
    except Exception as exc:
        logger.warning("SerpAPI exit background query 1 failed for '%s': %s", founder_name, exc)

    # Query 2: background/work history signals
    background_query = f'"{founder_name}" previously worked OR "formerly at" OR background'
    try:
        resp = requests.get(
            _SERP_API_URL,
            params={"q": background_query, "api_key": serpapi_key, "num": 5, "engine": "google"},
            timeout=_SERP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("organic_results", [])
        for r in results[:5]:
            snippet = r.get("snippet", "")
            if snippet:
                all_snippets.append(snippet)
    except Exception as exc:
        logger.warning("SerpAPI exit background query 2 failed for '%s': %s", founder_name, exc)

    if not all_snippets or not claude_client:
        return {"has_exit_background": False, "details": None}

    # Deduplicate and cap snippets
    unique_snippets = list(dict.fromkeys(all_snippets))[:5]
    snippet_text = "\n".join(f"[{i+1}] {s}" for i, s in enumerate(unique_snippets))

    system_prompt = (
        "You are a data extraction assistant. Analyze search result snippets and return ONLY valid JSON — "
        "no markdown, no explanation."
    )
    user_message = (
        f"Did {founder_name} previously work at or found a consumer brand that was acquired or had a notable exit? "
        f"Look for: worked at [Brand] which was acquired by [Company], or founded [Brand] which was acquired.\n\n"
        f"Search snippets:\n{snippet_text}\n\n"
        f'Return JSON: {{"has_exit_background": true/false, "details": "1-2 sentences max, or null"}}'
    )

    try:
        message = claude_client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=200,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            timeout=20,
        )
        text = message.content[0].text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        result = json.loads(text)
        return {
            "has_exit_background": bool(result.get("has_exit_background", False)),
            "details": result.get("details") or None,
        }
    except json.JSONDecodeError as exc:
        logger.warning("Exit background JSON parse error for '%s': %s", founder_name, exc)
        return {"has_exit_background": False, "details": None}
    except Exception as exc:
        logger.warning("Exit background search failed for '%s': %s", founder_name, exc)
        return {"has_exit_background": False, "details": None}


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
    4. Website scraping: find brand site → scrape About/Team page → extract founder via Claude Haiku
    5. Return name=None if nothing found

    After name is determined, also runs exit background search.

    Returns:
        {
            "name":             str | None,
            "confidence":       "high" | "medium" | "low" | "unknown",
            "source":           str,
            "linkedin_url_hint": str | None,
            "exit_background":  {"has_exit_background": bool, "details": str | None}
        }
    """
    serpapi_key = os.environ.get("SERPAPI_API_KEY", "").strip()
    category_str = category or ""

    # Try to get Claude client once for reuse
    claude_client = None
    try:
        from ..services.enrichment import _get_client
        claude_client = _get_client()
    except Exception as exc:
        logger.warning("Anthropic client unavailable in discover_founder: %s", exc)

    found_name = None
    found_confidence = "low"
    found_source = "unknown"
    linkedin_url_hint = None

    # ── 1. Filer name heuristic ───────────────────────────────────────────────
    if filer_name and looks_like_person(filer_name):
        logger.info("Founder discovery: filer_name '%s' looks like a person (high confidence)", filer_name)
        found_name = filer_name
        found_confidence = "high"
        found_source = "filer"

    # ── 2. Web search ─────────────────────────────────────────────────────────
    if not found_name:
        web_query = f'"{brand_name}" founder OR "founded by" OR CEO'
        web_snippets = _serp_search(web_query)
        if web_snippets:
            result = _extract_founder_from_snippets(brand_name, web_snippets)
            if result.get("name") and result.get("confidence") in ("high", "medium"):
                logger.info(
                    "Founder discovery: found '%s' via web search (confidence=%s)",
                    result["name"], result["confidence"],
                )
                found_name = result["name"]
                found_confidence = result["confidence"]
                found_source = "web_search"

    # ── 3. Product Hunt search ────────────────────────────────────────────────
    if not found_name:
        ph_query = f'site:producthunt.com "{brand_name}"'
        ph_snippets = _serp_search(ph_query)
        if ph_snippets:
            result = _extract_founder_from_snippets(brand_name, ph_snippets)
            if result.get("name") and result.get("confidence") in ("high", "medium"):
                logger.info(
                    "Founder discovery: found '%s' via Product Hunt search (confidence=%s)",
                    result["name"], result["confidence"],
                )
                found_name = result["name"]
                found_confidence = result["confidence"]
                found_source = "producthunt"

    # ── 4. Website scraping ───────────────────────────────────────────────────
    if not found_name or not linkedin_url_hint:
        try:
            website_url = find_brand_website(brand_name, category_str, serpapi_key)
            if website_url:
                page_text = scrape_about_page(website_url)
                if page_text and claude_client:
                    founders = extract_founders_from_page(page_text, brand_name, claude_client)
                    if founders:
                        first_founder = founders[0]
                        scraped_name = first_founder.get("name")
                        scraped_linkedin = first_founder.get("linkedin_url")

                        # Use scraped name only if cascade didn't find one
                        if not found_name and scraped_name:
                            logger.info(
                                "Founder discovery: found '%s' via website scraping",
                                scraped_name,
                            )
                            found_name = scraped_name
                            found_confidence = "medium"
                            found_source = "website"

                        # Store LinkedIn hint if found (saves Proxycurl credit)
                        if scraped_linkedin and not linkedin_url_hint:
                            linkedin_url_hint = scraped_linkedin
                            logger.info(
                                "Founder discovery: LinkedIn URL hint from website: %s",
                                scraped_linkedin,
                            )
        except Exception as exc:
            logger.warning("Website scraping step failed for '%s': %s", brand_name, exc)

    # ── 5. Exit background search ─────────────────────────────────────────────
    exit_background = {"has_exit_background": False, "details": None}
    if found_name:
        try:
            exit_background = search_exit_background(
                founder_name=found_name,
                brand_name=brand_name,
                serpapi_key=serpapi_key,
                claude_client=claude_client,
            )
        except Exception as exc:
            logger.warning("Exit background search failed for '%s': %s", found_name, exc)

    # ── 6. Nothing found ──────────────────────────────────────────────────────
    if not found_name:
        logger.info("Founder discovery: no confident match found for '%s'", brand_name)

    return {
        "name":              found_name,
        "confidence":        found_confidence if found_name else "unknown",
        "source":            found_source if found_name else "unknown",
        "linkedin_url_hint": linkedin_url_hint,
        "exit_background":   exit_background,
    }
