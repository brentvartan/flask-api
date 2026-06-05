"""
trademark_assignments.py
------------------------
USPTO Trademark Assignment chain resolver.

When a brand files a trademark under a stealth entity name (e.g. "Lightyear
Incorporated" for what will become "Filament"), the assignment record links
the stealth entity to the operating entity. This service resolves that chain
so we surface "Filament Sciences Inc" as the brand, not "Lightyear Inc."

USPTO Assignments API (free, no key):
  https://assignments.uspto.gov/assignments/assignment-service
  ?db=trademark&q.serial-no=SERIALNO

Also used to de-conflict placeholder names from operating names — the most
common cause of missed signals in the original spec backtest.
"""
import json
import logging
import re
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10
ASSIGNMENTS_URL = "https://assignments.uspto.gov/assignments/assignment-service"

# Regex to extract serial number from USPTO notes / description text
_SERIAL_RE = re.compile(r"\b(\d{8})\b")   # USPTO serial numbers are 8 digits


def _extract_serial_number(text: str) -> str | None:
    """Extract an 8-digit USPTO serial number from free text."""
    if not text:
        return None
    m = _SERIAL_RE.search(text)
    return m.group(1) if m else None


def fetch_assignment_chain(serial_number: str) -> dict:
    """
    Fetch trademark assignment records for a given serial number.

    Returns:
        {
            "serial_number": str,
            "assignments": [
                {
                    "assignor": str,   # original filer / stealth entity
                    "assignee": str,   # new owner / operating brand
                    "recorded_date": str,
                }
            ],
            "latest_assignee": str | None,   # most recent owner
            "has_assignment": bool,
            "error": str | None,
        }
    """
    url = (
        f"{ASSIGNMENTS_URL}"
        f"?db=trademark&q.serial-no={serial_number}"
        f"&output=json"
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "BullishStealthFinder/1.0 (contact: brent@bullish.co)",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {
            "serial_number": serial_number,
            "assignments": [],
            "latest_assignee": None,
            "has_assignment": False,
            "error": f"HTTP {e.code}",
        }
    except (urllib.error.URLError, json.JSONDecodeError, Exception) as e:
        return {
            "serial_number": serial_number,
            "assignments": [],
            "latest_assignee": None,
            "has_assignment": False,
            "error": str(e),
        }

    # Parse the USPTO assignment response
    # Response structure: {"assignmentResponse": {"assignments": [...]}}
    assignments_raw = (
        data.get("assignmentResponse", {})
            .get("assignments", {})
            .get("assignment", [])
    )
    if isinstance(assignments_raw, dict):
        assignments_raw = [assignments_raw]

    assignments = []
    for rec in assignments_raw:
        assignors = rec.get("assignors", {}).get("assignor", [])
        assignees = rec.get("assignees", {}).get("assignee", [])
        if isinstance(assignors, dict):
            assignors = [assignors]
        if isinstance(assignees, dict):
            assignees = [assignees]

        for assignor in assignors:
            for assignee in assignees:
                assignments.append({
                    "assignor":      assignor.get("name", ""),
                    "assignee":      assignee.get("name", ""),
                    "recorded_date": rec.get("recordedDate", ""),
                })

    latest_assignee = assignments[-1]["assignee"] if assignments else None

    return {
        "serial_number":  serial_number,
        "assignments":    assignments,
        "latest_assignee": latest_assignee,
        "has_assignment": bool(assignments),
        "error":          None,
    }


def resolve_brand_name(
    original_owner: str,
    notes: str = "",
    description: str = "",
) -> dict:
    """
    Attempt to resolve the true brand name from a trademark signal.

    Extracts a serial number from notes/description, then checks the
    assignment chain. If the mark was assigned to a different entity,
    return the assignee name as the resolved brand.

    Returns:
        {
            "resolved_name": str | None,  # assignee if different from original
            "stealth_entity": str | None,  # original filer name if reassigned
            "serial_number": str | None,
            "has_assignment": bool,
        }
    """
    serial = _extract_serial_number(notes) or _extract_serial_number(description)
    if not serial:
        return {
            "resolved_name": None,
            "stealth_entity": None,
            "serial_number": None,
            "has_assignment": False,
        }

    chain = fetch_assignment_chain(serial)

    if chain.get("error") or not chain.get("has_assignment"):
        return {
            "resolved_name": None,
            "stealth_entity": None,
            "serial_number": serial,
            "has_assignment": False,
        }

    latest = chain.get("latest_assignee", "")

    # Only flag as a reassignment if the assignee name differs meaningfully
    # from the original owner (guards against administrative transfers)
    if latest and latest.lower() != original_owner.lower():
        logger.info(
            "Trademark assignment: '%s' → '%s' (serial %s)",
            original_owner, latest, serial,
        )
        return {
            "resolved_name": latest,
            "stealth_entity": original_owner,
            "serial_number": serial,
            "has_assignment": True,
        }

    return {
        "resolved_name": None,
        "stealth_entity": None,
        "serial_number": serial,
        "has_assignment": False,
    }
