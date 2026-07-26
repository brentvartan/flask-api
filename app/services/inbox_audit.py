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

# NOTE: there was a module-level _latest_audit cache here. It was removed on
# 2026-07-25 — with 4 gunicorn workers the answer depended on which worker served
# the request, and it was lost on every deploy. Audit results are read from the DB.


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
    """
    Persist the audit result as an Item — one row per run, so coverage is a SERIES.

    History worth knowing: this used to pass `name=` to Item, which has no `name`
    column (only `title`). The create branch raised TypeError, the bare `except`
    downgraded it to a warning, and because nothing was ever written the update
    branch was never reached either — so it failed the same way every time. No
    inbox_audit row has ever existed. The 30.4% coverage figure lived only in the
    _latest_audit module global: per-gunicorn-worker (there are 4), and gone on every
    deploy. Coverage is the number this product is judged by; it has to be durable.

    Appends rather than overwrites. A single number tells you nothing about whether
    the engine work helped; two numbers 30 days apart do.

    Deliberately does NOT swallow exceptions. The caller is an admin route, so a
    failure here should surface as a 500 rather than a log line nobody reads —
    silently failing to record the metric is exactly how this went unnoticed.
    """
    from ..models.item import Item
    from ..models.user import User
    from ..extensions import db

    with app.app_context():
        admin = User.query.filter_by(role='admin').first()
        if not admin:
            logger.error("Inbox audit NOT stored: no admin user exists")
            return

        db.session.add(Item(
            title=f"Inbox Audit — {result['run_at'][:10]}",
            description=json.dumps(result),
            item_type='inbox_audit',
            owner_id=admin.id,
        ))
        db.session.commit()
        logger.info(
            "Inbox audit stored: %d checked, %d missing, %.1f%% coverage",
            result['total_checked'], result['missing_count'], result['coverage_pct'],
        )


def get_latest_audit(app) -> dict | None:
    """
    Return the most recent audit result, always from the DB.

    No module-level cache: with 4 gunicorn workers a process-local global meant the
    answer depended on which worker served the request, and it vanished on deploy.
    """
    try:
        from ..models.item import Item
        from ..models.user import User

        with app.app_context():
            admin = User.query.filter_by(role='admin').first()
            if not admin:
                return None
            item = (
                Item.query
                .filter_by(item_type='inbox_audit', owner_id=admin.id)
                .order_by(Item.created_at.desc())
                .first()
            )
            if item and item.description:
                return json.loads(item.description)
    except Exception as exc:
        logger.warning("Failed to retrieve inbox audit: %s", exc)


def get_audit_history(app, limit: int = 24) -> list[dict]:
    """
    Return audit runs newest-first. The point of appending is being able to compare
    a coverage number against the previous one, which needs more than the latest row.
    """
    from ..models.item import Item
    from ..models.user import User

    with app.app_context():
        admin = User.query.filter_by(role='admin').first()
        if not admin:
            return []
        rows = (
            Item.query
            .filter_by(item_type='inbox_audit', owner_id=admin.id)
            .order_by(Item.created_at.desc())
            .limit(limit)
            .all()
        )
        out = []
        for row in rows:
            try:
                out.append(json.loads(row.description or "{}"))
            except json.JSONDecodeError:
                logger.warning("Skipping unparseable inbox_audit row id=%s", row.id)
        return out

    return None
