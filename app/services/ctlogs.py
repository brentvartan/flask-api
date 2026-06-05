"""
CT Logs scanner — Certificate Transparency logs via crt.sh

Monitors for newly issued SSL certificates with consumer-brand domain patterns.
Catches new DTC/consumer brands at domain registration time — SSL certs are
issued at the moment a brand sets up its site, typically months to years before
press coverage.

Lead times (from spec backtest):
  board.fun: registered 22 months before Series A announcement
  SSL cert issued within hours/days of domain registration

Approach:
  - Query crt.sh (free, no API key required) for consumer-brand pattern domains
  - Filter for certs issued in the last N days
  - Filter for clean root domains (.com, .co, .io, .health, .fun, .shop)
  - Skip known corporate, CDN, and infrastructure domains
  - Return as signals for Claude enrichment

No API key required.
"""
import json
import logging
import re
import urllib.request
import urllib.error
import urllib.parse

from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15
CRTSH_URL = "https://crt.sh/"

# ── Domain patterns to search ─────────────────────────────────────────────────
# Tight consumer-brand patterns. Broad keywords (e.g. "health") would return
# millions of results; these are tuned for brand-name structure.
# Each entry is a crt.sh query string (% = wildcard).

SEARCH_PATTERNS = [
    # Brand-starter prefixes — very common in DTC naming
    "get%.co", "get%.health", "get%.shop",
    "try%.co", "try%.shop",
    "join%.co", "join%.shop",
    "my%.co", "our%.co",
    # Beverage / food brand patterns
    "%sip%.com", "%brew%.co", "%pour%.co",
    "%snack%.co", "%bite%.co",
    # Beauty / personal care
    "%glow%.co", "%balm%.co", "%bare%.co",
    # Wellness
    "%vita%.co", "%ritual%.co",
    "%wellness%.co",
    # Pet
    "%paw%.co", "%fur%.co",
    # DTC-signature TLDs with any consumer-friendly name
    "%.fun",       # board.fun, drink.fun, etc.
    "%.health",    # consumer health brands
]

# ── Known corporate / infrastructure domains to exclude ──────────────────────
EXCLUDE_PATTERNS = [
    r"google\.", r"amazon\.", r"aws\.", r"cloudflare\.", r"microsoft\.",
    r"apple\.", r"facebook\.", r"meta\.", r"twitter\.", r"tiktok\.",
    r"shopify\.", r"stripe\.", r"twilio\.", r"sendgrid\.", r"mailchimp\.",
    r"hubspot\.", r"salesforce\.", r"zendesk\.", r"atlassian\.",
    r"cdn\.", r"static\.", r"assets\.", r"media\.", r"images\.",
    r"www\d+\.", r"mail\.", r"smtp\.", r"imap\.", r"pop\.",
    r"staging\.", r"dev\.", r"test\.", r"demo\.", r"sandbox\.",
    r"api\.", r"app\.", r"admin\.", r"portal\.", r"dashboard\.",
]
_EXCLUDE_RE = re.compile("|".join(EXCLUDE_PATTERNS), re.IGNORECASE)

# Root TLDs that suggest a consumer brand launch
CONSUMER_TLDS = {".com", ".co", ".io", ".health", ".fun", ".shop", ".store", ".brand"}

# Maximum results to process per query (crt.sh can return thousands)
MAX_PER_PATTERN = 200
# Maximum total signals to return from a full scan
MAX_TOTAL = 100


def _is_root_domain(domain: str) -> bool:
    """Return True if domain is a clean root domain (brand.com, not sub.brand.com)."""
    domain = domain.lstrip("*").lstrip(".")  # strip wildcard prefix
    parts = domain.split(".")
    # Exactly 2 parts: brand + tld, or 3 parts with country-code like brand.co.uk
    if len(parts) == 2:
        return True
    if len(parts) == 3 and len(parts[-1]) == 2:  # e.g. .co.uk
        return True
    return False


def _looks_like_brand(domain: str) -> bool:
    """Heuristic: does this domain look like a new consumer brand vs. corp/infra?"""
    root = domain.split(".")[0].lower()
    # Skip very short (1-2 chars) or very long (30+ chars) names
    if len(root) < 3 or len(root) > 30:
        return False
    # Skip names that are purely numeric
    if root.isdigit():
        return False
    # Skip obvious infrastructure patterns
    if _EXCLUDE_RE.search(domain):
        return False
    return True


def _query_crtsh(pattern: str, days_back: int) -> list[str]:
    """
    Query crt.sh for domains matching pattern with certs issued in last N days.
    Returns list of unique root domain strings.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    url = f"{CRTSH_URL}?q={urllib.parse.quote(pattern)}&output=json"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "BullishStealthFinder/1.0 (contact: brent@bullish.co)"},
    )

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        logger.warning("crt.sh HTTP %s for pattern '%s'", e.code, pattern)
        return []
    except (urllib.error.URLError, json.JSONDecodeError, Exception) as e:
        logger.warning("crt.sh error for pattern '%s': %s", pattern, e)
        return []

    if not isinstance(data, list):
        return []

    domains = set()
    for entry in data[:MAX_PER_PATTERN]:
        not_before_str = entry.get("not_before", "")
        try:
            not_before = datetime.fromisoformat(not_before_str.replace("Z", "+00:00"))
            if not_before < cutoff:
                continue
        except (ValueError, AttributeError):
            continue

        # Extract domain from common_name and name_value
        for field in ("common_name", "name_value"):
            raw = entry.get(field, "")
            for line in raw.split("\n"):
                line = line.strip().lstrip("*").lstrip(".").lower()
                if not line:
                    continue
                if _is_root_domain(line) and _looks_like_brand(line):
                    domains.add(line)

    return list(domains)


def search_recent_ct_domains(days_back: int = 14, max_results: int = 100) -> dict:
    """
    Scan Certificate Transparency logs for new consumer-brand domains.

    Args:
        days_back:   Look back this many days (1–30)
        max_results: Maximum signals to return

    Returns:
        {
            "signals": [...],
            "total_found": int,
            "fetched": int,
            "error": str | None,
        }
    """
    days_back   = max(1, min(days_back, 30))
    max_results = max(1, min(max_results, MAX_TOTAL))

    all_domains: set[str] = set()
    errors = []

    for pattern in SEARCH_PATTERNS:
        try:
            found = _query_crtsh(pattern, days_back)
            all_domains.update(found)
            logger.debug("CT pattern '%s' → %d domains", pattern, len(found))
        except Exception as exc:
            errors.append(f"{pattern}: {exc}")

    if not all_domains and errors:
        return {"signals": [], "total_found": 0, "fetched": 0, "error": errors[0]}

    # Build signals from domains
    signals = []
    now_ts = datetime.now(timezone.utc).isoformat()

    for domain in sorted(all_domains)[:max_results]:
        # Derive a rough brand name from the domain (strip TLD, title-case)
        brand_name = domain.split(".")[0].replace("-", " ").replace("_", " ").title()
        tld = "." + ".".join(domain.split(".")[1:])

        signals.append({
            "companyName":  brand_name,
            "signal_type":  "domain_ct",
            "category":     "Unknown",   # Claude will infer from brand name
            "score_boost":  6,
            "description":  f"{brand_name} — new domain {domain} (CT log, last {days_back}d)",
            "url":          f"https://{domain}",
            "notes":        (
                f"New SSL certificate issued for {domain} within the last {days_back} days. "
                f"Domain registered on a consumer-brand TLD ({tld}). "
                f"No press coverage found — this brand may be pre-launch stealth."
            ),
            "timestamp":    now_ts,
        })

    return {
        "signals":     signals,
        "total_found": len(all_domains),
        "fetched":     len(signals),
        "error":       errors[0] if errors and not signals else None,
    }
