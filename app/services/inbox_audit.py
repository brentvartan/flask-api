"""
Monthly Deals Inbox Audit Service.

Checks brand names extracted from the Gmail "INVESTMENTS/A. Deals" inbox
against the Stealth Finder signal database. Identifies consumer brands that
appeared in deal flow but were never surfaced by the scanner — measuring
how tight the gating system is.

Triggered monthly via APScheduler. Can also be called directly via the
POST /api/admin/inbox-audit/run admin endpoint (brands supplied externally,
e.g. by a Claude Code scheduled task that reads Gmail via MCP).
"""
import json
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Cached in-memory — survives within a Railway process lifecycle.
# The admin endpoint also writes results to the DB as an Item so they
# persist across restarts.
_latest_audit: dict | None = None


def _normalize(name: str) -> str:
    """Lowercase, strip punctuation/legal suffixes for fuzzy matching."""
    name = name.lower()
    name = re.sub(r'\b(llc|inc|corp|ltd|co|the|a|an)\b\.?', '', name)
    name = re.sub(r'[^a-z0-9 ]', '', name)
    return name.strip()


def _search_keyword(keyword: str, app) -> bool:
    """Return True if *keyword* appears in any signal description in the DB."""
    from ..models.item import Item
    kw = f'%{keyword}%'
    hit = (
        Item.query
        .filter(
            Item.item_type == 'signal',
            Item.description.ilike(kw),
        )
        .first()
    )
    return hit is not None


def check_brands_in_db(brand_names: list[str], app) -> dict:
    """
    For each brand name, check whether it exists in the signal DB.

    Returns a dict with found/missing lists and summary stats.
    """
    with app.app_context():
        found = []
        missing = []

        for raw_name in brand_names:
            if not raw_name or not raw_name.strip():
                continue

            name = raw_name.strip()
            norm = _normalize(name)

            # Use the most distinctive word(s) as the search key.
            # Skip common stop words for the search term.
            stop = {"the", "a", "an", "by", "and", "or", "of", "for", "in"}
            words = [w for w in norm.split() if w and w not in stop and len(w) > 2]

            if not words:
                missing.append({"name": name, "reason": "too generic to search"})
                continue

            # Try the first two meaningful words (most specific)
            search_term = ' '.join(words[:2]) if len(words) > 1 else words[0]
            in_db = _search_keyword(search_term, app)

            # If two-word miss, try just the first word
            if not in_db and len(words) > 1:
                in_db = _search_keyword(words[0], app)

            if in_db:
                found.append({"name": name, "search_term": search_term})
            else:
                missing.append({"name": name, "search_term": search_term, "reason": "not in signal DB"})

        total = len(found) + len(missing)
        result = {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "total_checked": total,
            "found_count": len(found),
            "missing_count": len(missing),
            "coverage_pct": round(len(found) / total * 100, 1) if total else 0,
            "found": found,
            "missing": missing,
        }

        _store_audit_result(result, app)
        return result


def _store_audit_result(result: dict, app) -> None:
    """Persist the audit result as an Item so it survives Railway restarts."""
    global _latest_audit
    _latest_audit = result

    try:
        from ..models.item import Item
        from ..models.user import User
        from ..extensions import db

        with app.app_context():
            admin = User.query.filter_by(role='admin').first()
            if not admin:
                return

            # Overwrite any existing audit record (keep only the latest)
            existing = Item.query.filter_by(item_type='inbox_audit', owner_id=admin.id).first()
            if existing:
                existing.description = json.dumps(result)
                existing.name = f"Inbox Audit — {result['run_at'][:10]}"
            else:
                audit_item = Item(
                    name=f"Inbox Audit — {result['run_at'][:10]}",
                    description=json.dumps(result),
                    item_type='inbox_audit',
                    owner_id=admin.id,
                )
                db.session.add(audit_item)
            db.session.commit()
            logger.info("Inbox audit stored: %d checked, %d missing", result['total_checked'], result['missing_count'])
    except Exception as exc:
        logger.warning("Failed to store inbox audit result: %s", exc)


def get_latest_audit(app) -> dict | None:
    """Return the most recent audit result (memory → DB fallback)."""
    global _latest_audit
    if _latest_audit:
        return _latest_audit

    try:
        from ..models.item import Item
        from ..models.user import User

        with app.app_context():
            admin = User.query.filter_by(role='admin').first()
            if not admin:
                return None
            item = Item.query.filter_by(item_type='inbox_audit', owner_id=admin.id).first()
            if item and item.description:
                _latest_audit = json.loads(item.description)
                return _latest_audit
    except Exception as exc:
        logger.warning("Failed to retrieve inbox audit: %s", exc)

    return None
