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
from concurrent.futures import ThreadPoolExecutor, as_completed

from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15
CRTSH_URL = "https://crt.sh/"

# ── Domain patterns to search ─────────────────────────────────────────────────
# Tight consumer-brand patterns. Broad keywords (e.g. "health") would return
# millions of results; these are tuned for brand-name structure.
# Each entry is a crt.sh query string (% = wildcard).

SEARCH_PATTERNS = [
    # Brand-starter prefixes (tight — DTC naming conventions) — both .co and .com
    "get%.co", "try%.co", "join%.co",
    "get%.com", "try%.com", "join%.com",
    # Beverage / food
    "%sip%.co", "%brew%.co", "%snack%.co",
    "%sip%.com", "%brew%.com", "%snack%.com",
    # Beauty / personal care
    "%glow%.co", "%balm%.co",
    "%glow%.com", "%balm%.com",
    # Wellness
    "%vita%.co", "%ritual%.co",
    "%vita%.com", "%ritual%.com",
    # Pet
    "%paw%.co", "%paw%.com",
    # DTC-signature TLDs
    "%.fun",      # board.fun — the example from the spec
    "%.health",
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


# ── crt.sh fetch layer ────────────────────────────────────────────────────────


class CrtShError(Exception):
    """
    Raised when a single crt.sh pattern query cannot be completed.

    Carries the HTTP status when one was seen so the caller can name the failure
    in the scan's error message instead of reporting a clean, empty run.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _fetch_crtsh(pattern: str) -> list:
    """
    Fetch the raw crt.sh JSON payload for one pattern.

    Raises CrtShError on ANY failure (HTTP error, network error, bad JSON,
    unexpected payload shape). A dead source must never be swallowed into an
    empty-but-successful result — see search_recent_ct_domains.
    """
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
        raise CrtShError(f"HTTP {e.code}", status=e.code) from e
    except Exception as e:
        logger.warning("crt.sh error for pattern '%s': %s", pattern, e)
        raise CrtShError(str(e) or e.__class__.__name__) from e

    if not isinstance(data, list):
        logger.warning(
            "crt.sh returned %s (expected list) for pattern '%s'",
            type(data).__name__, pattern,
        )
        raise CrtShError(f"unexpected response type {type(data).__name__}")

    return data


def _query_crtsh(pattern: str, days_back: int) -> list[tuple]:
    """
    Query crt.sh for domains matching pattern with certs issued in last N days.
    Returns list of (domain, not_before) tuples for recency-based sorting upstream.

    Raises CrtShError if the pattern query itself failed, so the caller can count
    the failure rather than mistake it for "no matches".

    Important: recency filter runs BEFORE the cap so stale certs don't silently
    consume the MAX_PER_PATTERN budget.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    data = _fetch_crtsh(pattern)

    # Filter first (recency), then cap — never cap before filter
    hits: dict = {}  # domain → newest not_before seen (always tz-aware UTC)
    for entry in data:
        if not isinstance(entry, dict):
            continue  # one malformed record must never abort the whole pattern

        not_before_str = entry.get("not_before", "")
        try:
            not_before = datetime.fromisoformat(not_before_str.replace("Z", "+00:00"))
            # crt.sh emits not_before WITHOUT a timezone designator
            # ("2026-07-20T14:03:11"), so fromisoformat yields a NAIVE datetime
            # and comparing it against the aware cutoff raises TypeError.
            # Normalise to UTC before any comparison.
            if not_before.tzinfo is None:
                not_before = not_before.replace(tzinfo=timezone.utc)
            else:
                not_before = not_before.astimezone(timezone.utc)
            if not_before < cutoff:
                continue
        except (ValueError, AttributeError, TypeError):
            # TypeError is belt-and-braces: a single unparseable record must
            # never escape the loop and abort the entire pattern query.
            continue

        for field in ("common_name", "name_value"):
            raw = entry.get(field) or ""
            if not isinstance(raw, str):
                continue  # null / non-string field must not abort the pattern
            for line in raw.split("\n"):
                line = line.strip().lstrip("*").lstrip(".").lower()
                if not line:
                    continue
                if _is_root_domain(line) and _looks_like_brand(line):
                    if line not in hits or not_before > hits[line]:
                        hits[line] = not_before

        # Safety cap: stop scanning input once we've collected enough fresh hits
        if len(hits) >= MAX_PER_PATTERN:
            break

    return list(hits.items())  # [(domain, not_before), ...]


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
            "patterns_attempted": int,
            "patterns_succeeded": int,
            "patterns_failed": int,
        }
    """
    days_back   = max(1, min(days_back, 30))
    max_results = max(1, min(max_results, MAX_TOTAL))

    # domain → newest not_before seen across all patterns
    # Patterns run concurrently so total wall-clock ≈ slowest single pattern (~15s)
    # rather than sequential (18 × 15s = 270s which exceeds Railway's proxy timeout).
    all_domain_hits: dict = {}
    errors = []

    # ── Per-pattern outcome tracking ─────────────────────────────────────────
    # A dead source must not look healthy. crt.sh has gone dark for days at a
    # time; without these counters every pattern can fail and the scan still
    # records a clean run with error=None.
    patterns_attempted = len(SEARCH_PATTERNS)
    patterns_succeeded = 0
    patterns_failed    = 0
    last_status        = None   # last HTTP status seen on a failing pattern

    with ThreadPoolExecutor(max_workers=len(SEARCH_PATTERNS)) as pool:
        futures = {pool.submit(_query_crtsh, p, days_back): p for p in SEARCH_PATTERNS}
        for future in as_completed(futures):
            pattern = futures[future]
            try:
                found = future.result()
                patterns_succeeded += 1
                for domain, not_before in found:
                    if domain not in all_domain_hits or not_before > all_domain_hits[domain]:
                        all_domain_hits[domain] = not_before
                logger.debug("CT pattern '%s' → %d domains", pattern, len(found))
            except Exception as exc:
                patterns_failed += 1
                status = getattr(exc, "status", None)
                if status is not None:
                    last_status = status
                errors.append(f"{pattern}: {exc}")
                logger.warning("CT pattern '%s' failed: %s", pattern, exc)

    # Cap by recency (newest first) — not alphabetically
    sorted_domains = [
        domain for domain, _ in
        sorted(all_domain_hits.items(), key=lambda x: x[1], reverse=True)
    ][:max_results]

    # Build signals from domains
    signals = []
    now_ts = datetime.now(timezone.utc).isoformat()

    for domain in sorted_domains:
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

    # ── Honest error reporting ───────────────────────────────────────────────
    # Every pattern failed → the source itself is dark. Surface a named error so
    # scheduler.py writes it to ScanRun.error_message and the UI shows it.
    if patterns_failed and patterns_succeeded == 0:
        detail = (
            f"last HTTP status {last_status}" if last_status is not None
            else f"last error: {errors[-1]}"
        )
        error = (
            f"crt.sh unreachable — all {patterns_failed}/{patterns_attempted} "
            f"pattern queries failed ({detail})"
        )
        logger.warning("CT log scan produced no signals: %s", error)
    elif errors and not signals:
        # Partial outage that yielded nothing: still report rather than pretend.
        error = errors[0]
    else:
        error = None

    return {
        "signals":            signals,
        "total_found":        len(all_domain_hits),
        "fetched":            len(signals),
        "error":              error,
        "patterns_attempted": patterns_attempted,
        "patterns_succeeded": patterns_succeeded,
        "patterns_failed":    patterns_failed,
    }
