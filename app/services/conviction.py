"""
conviction.py
-------------
Founder-designation entries from data/watchlist_seed.csv, matched against
the free text already flowing through signals:
  - USPTO trademark owner/signatory fields
  - Form D "related persons" fields
  - Press release bylines and body text (newswire signals)
  - Product Hunt / App Store creator fields

A conviction match surfaces in the signal as `conviction_match` and bumps the
signal to the top of the digest. It is NEVER a gate or exclusion — unknown
first-timers still surface, just ranked below conviction matches.

---
HOW TO ADD A FOUNDER:
  Add a row to data/watchlist_seed.csv with designation=founder.
  Both first and last name must appear in target text (prevents false matches).
  Use the name as it would appear in a legal filing or press byline.
"""
import csv
import os
import re
import logging

logger = logging.getLogger(__name__)

_CSV_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'watchlist_seed.csv')
)


def _load_watchlist_founders() -> dict[str, tuple[str, list[str]]]:
    """Load founder-designation rows from watchlist_seed.csv as a conviction dict."""
    result: dict[str, tuple[str, list[str]]] = {}
    try:
        with open(_CSV_PATH, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                if row.get('designation', '').strip().lower() != 'founder':
                    continue
                name = row.get('name', '').strip()
                if not name:
                    continue
                exit_brand = row.get('exit_brand', '').strip()
                role       = row.get('role', '').strip()
                exit_type  = row.get('exit_type', '').strip()
                exit_year  = row.get('exit_year', '').strip()
                parts  = [p for p in [role, exit_brand] if p]
                reason = ' at '.join(parts) if parts else name
                if exit_type or exit_year:
                    suffix = ' '.join(p for p in [exit_type, exit_year] if p)
                    reason += f" ({suffix})"
                result[name] = (reason, [exit_brand] if exit_brand else [])
    except FileNotFoundError:
        logger.warning(
            "watchlist_seed.csv not found at %s — conviction list will be empty", _CSV_PATH
        )
    except Exception as exc:
        logger.warning(
            "Could not load watchlist_seed.csv: %s — conviction list will be empty", exc
        )
    return result


# Populated at import time from data/watchlist_seed.csv (designation=founder rows).
# Prior hard-coded contents archived in docs/archive/people_lists_pre_reset_2026-07.md.
CONVICTION_FOUNDERS: dict[str, tuple[str, list[str]]] = _load_watchlist_founders()


def check_conviction_match(text: str) -> dict | None:
    """
    Check if any conviction founder's name appears in the given text.

    Matches both "Firstname Lastname" and "Lastname, Firstname" forms.
    Both first and last name must be present (guards against common first names).
    Case-insensitive.

    Returns:
        {"name": str, "reason": str, "known_brands": list[str], "designation": "founder"}
        or None
    """
    if not text:
        return None

    text_lower = text.lower()

    for full_name, (reason, known_brands) in CONVICTION_FOUNDERS.items():
        parts = full_name.strip().split()
        if len(parts) < 2:
            continue
        first = parts[0].lower()
        last  = parts[-1].lower()

        if first in text_lower and last in text_lower:
            logger.info("Conviction match: '%s' found in signal text", full_name)
            return {
                "name":         full_name,
                "reason":       reason,
                "known_brands": known_brands,
                "designation":  "founder",
            }

    return None


def check_conviction_match_multi(texts: list[str]) -> dict | None:
    """
    Check multiple text fields (e.g. owner, notes, description) for a match.
    Returns the first match found, or None.
    """
    for text in texts:
        match = check_conviction_match(text or "")
        if match:
            return match
    return None
