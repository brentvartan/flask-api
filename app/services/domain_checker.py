"""
Domain validator for Stealth Finder.

Crawls domain signal URLs and classifies them as:
  live        — real site with meaningful content
  coming_soon — placeholder / waitlist / launch page
  parked      — domain parking page (for sale, sedo, godaddy, etc.)
  error       — non-200, timeout, or connection failure

Uses only stdlib (urllib) — no new dependencies.
"""
import re
import logging
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
_TIMEOUT = 10
_MAX_BYTES = 65536  # read first 64 KB only

_COMING_SOON_RE = re.compile(
    r"coming\s+soon|launching\s+soon|under\s+construction|stay\s+tuned"
    r"|be\s+the\s+first|opening\s+soon|waitlist|wait\s+list"
    r"|notify\s+me|get\s+notified|join.*waitlist|early\s+access"
    r"|sign\s+up.*launch|something.{0,20}coming|coming\s+\d{4}",
    re.IGNORECASE,
)

_PARKING_RE = re.compile(
    r"domain\s+for\s+sale|buy\s+this\s+domain|this\s+domain\s+is\s+for\s+sale"
    r"|domain\s+may\s+be\s+for\s+sale|parked\s+(by|at|domain)"
    r"|godaddy\.com|sedo\.com|afternic\.com|dan\.com"
    r"|undeveloped\.com|hugedomains\.com"
    r"|this\s+web\s+page\s+is\s+parked|web\s+hosting\s+provider"
    r"|bluehost\.com|hostgator\.com",
    re.IGNORECASE,
)

_EMAIL_CAPTURE_RE = re.compile(
    r'<input[^>]+type=["\']email["\']'
    r'|<input[^>]+placeholder=["\'][^"\']*email[^"\']*["\']'
    r'|subscribe.*?email|enter.*?email.*?address|email.*?newsletter',
    re.IGNORECASE,
)

_SOCIAL_RE = re.compile(
    r'href=["\']('
    r'https?://(?:www\.)?(?:instagram|twitter|x|tiktok|facebook|linkedin)\.com/[^"\'/?# ]+'
    r')["\']',
    re.IGNORECASE,
)

_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.IGNORECASE | re.DOTALL)
_META_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']'
    r'|<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']description["\']',
    re.IGNORECASE,
)


def check_domain_status(url: str) -> dict:
    """
    Crawl *url* and return a classification dict.

    Result schema:
        {
            "status":           "live" | "coming_soon" | "parked" | "error",
            "page_title":       str | None,
            "meta_description": str | None,
            "has_email_capture": bool,
            "social_links":     list[str],
            "checked_at":       str,   # ISO-8601 UTC
        }
    """
    result = {
        "status":           "error",
        "page_title":       None,
        "meta_description": None,
        "has_email_capture": False,
        "social_links":     [],
        "checked_at":       datetime.now(timezone.utc).isoformat(),
    }

    if not url or not url.startswith("http"):
        return result

    try:
        req = Request(url, headers={"User-Agent": _UA})
        with urlopen(req, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                return result
            ct = resp.headers.get("Content-Type", "")
            if "html" not in ct.lower():
                result["status"] = "live"
                return result
            raw = resp.read(_MAX_BYTES)
    except (HTTPError, URLError, OSError, Exception) as exc:
        logger.debug("Domain check failed for %s: %s", url, exc)
        return result

    try:
        html = raw.decode("utf-8", errors="replace")
    except Exception:
        html = raw.decode("latin-1", errors="replace")

    # Title
    m = _TITLE_RE.search(html)
    if m:
        result["page_title"] = re.sub(r"\s+", " ", m.group(1).strip())[:200]

    # Meta description
    m = _META_DESC_RE.search(html)
    if m:
        result["meta_description"] = (m.group(1) or m.group(2) or "").strip()[:500]

    # Email capture
    result["has_email_capture"] = bool(_EMAIL_CAPTURE_RE.search(html))

    # Social links
    result["social_links"] = list(set(_SOCIAL_RE.findall(html)))[:10]

    # Classification
    if _PARKING_RE.search(html):
        result["status"] = "parked"
    elif _COMING_SOON_RE.search(html):
        result["status"] = "coming_soon"
    else:
        result["status"] = "live"

    return result
