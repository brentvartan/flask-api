"""
Confluence Detection Service

When a new signal is saved, this service:
  1. Normalises the brand name into a stable brand_key
  2. Records the signal in signal_events
  3. Checks if a NEW distinct signal type has appeared for this brand
  4. If yes → creates a ConfluenceHit and fires an alert email

Brand key normalisation strips legal suffixes (LLC, INC, CORP…) and
punctuation so "NEURO GUM LLC" and "NEURO GUM" match the same key.
"""
import json
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Legal suffixes to strip before matching
_LEGAL_SUFFIXES = re.compile(
    r'\b(LLC|INC|CORP|LTD|LIMITED|LP|LLP|CO|COMPANY|INCORPORATED|'
    r'CORPORATION|HOLDINGS|GROUP|BRANDS|VENTURES|STUDIO|STUDIOS)\b\.?',
    re.IGNORECASE,
)
_NON_ALPHA = re.compile(r'[^A-Z0-9 ]')


def normalize_brand(name: str) -> str:
    """Return a lowercase slug used for cross-signal matching."""
    if not name:
        return ""
    upper = name.upper().strip()
    stripped = _LEGAL_SUFFIXES.sub('', upper)
    cleaned = _NON_ALPHA.sub('', stripped)
    return re.sub(r'\s+', ' ', cleaned).strip().lower()


def record_signal_and_check_confluence(
    item_id: int,
    owner_id: int,
    brand_name: str,
    signal_type: str,
    source_url: str = None,
    enrichment: dict = None,
) -> dict:
    """
    Record a new signal event and check for confluence.

    Returns:
        {
            "is_confluence": bool,
            "signal_count":  int,
            "signal_types":  list[str],
            "hit_id":        int | None,
        }
    """
    from ..extensions import db
    from ..models.signal_event import SignalEvent
    from ..models.confluence_hit import ConfluenceHit

    brand_key = normalize_brand(brand_name)
    if not brand_key:
        return {"is_confluence": False, "signal_count": 1, "signal_types": [signal_type], "hit_id": None}

    # ── 1. Record this signal ─────────────────────────────────────────────────
    event = SignalEvent(
        item_id=item_id,
        owner_id=owner_id,
        brand_key=brand_key,
        brand_name=brand_name,
        signal_type=signal_type,
        source_url=source_url,
        detected_at=datetime.now(timezone.utc),
    )
    db.session.add(event)
    db.session.flush()  # get id without committing

    # ── 2. Count distinct signal types for this brand (including new one) ─────
    existing = (
        SignalEvent.query
        .filter_by(owner_id=owner_id, brand_key=brand_key)
        .with_entities(SignalEvent.signal_type)
        .all()
    )
    all_types = list({row.signal_type for row in existing} | {signal_type})
    signal_count = len(all_types)

    # ── 3. Only fire confluence if we've gained a NEW signal type ─────────────
    # (i.e. the previous max distinct count was one less than current)
    previous_count = signal_count - 1
    is_confluence = previous_count >= 1  # 1→2 or 2→3 etc. (at least one existed before)

    if not is_confluence:
        db.session.commit()
        return {"is_confluence": False, "signal_count": signal_count, "signal_types": all_types, "hit_id": None}

    # ── 4. Log the confluence hit ─────────────────────────────────────────────
    bullish_score = enrichment.get("bullish_score") if enrichment else None
    watch_level   = enrichment.get("watch_level")   if enrichment else None

    hit = ConfluenceHit(
        owner_id=owner_id,
        brand_key=brand_key,
        brand_name=brand_name,
        signal_count=signal_count,
        signal_types=json.dumps(sorted(all_types)),
        bullish_score=bullish_score,
        watch_level=watch_level,
        alert_sent=False,
    )
    db.session.add(hit)
    db.session.commit()

    logger.info(
        "⚡ Confluence: %s — %d signals %s (score: %s)",
        brand_name, signal_count, all_types, bullish_score,
    )

    return {
        "is_confluence": True,
        "signal_count":  signal_count,
        "signal_types":  all_types,
        "hit_id":        hit.id,
    }


def send_confluence_alert_for_hit(hit_id: int, alert_emails: list) -> bool:
    """
    Send the confluence alert email for a given ConfluenceHit id.
    Marks hit.alert_sent = True on success.
    """
    from ..extensions import db
    from ..models.confluence_hit import ConfluenceHit
    from ..models.signal_event import SignalEvent
    from ..services.email import send_confluence_alert

    hit = db.session.get(ConfluenceHit, hit_id)
    if not hit or hit.alert_sent:
        return False

    # Build timeline: one row per signal type with earliest detection date
    events = (
        SignalEvent.query
        .filter_by(owner_id=hit.owner_id, brand_key=hit.brand_key)
        .order_by(SignalEvent.detected_at.asc())
        .all()
    )

    # Deduplicate to first occurrence per signal_type
    seen = {}
    for ev in events:
        if ev.signal_type not in seen:
            seen[ev.signal_type] = ev

    timeline = [
        {
            "signal_type": ev.signal_type,
            "detected_at": ev.detected_at.strftime("%b %d, %Y"),
            "source_url":  ev.source_url,
        }
        for ev in seen.values()
    ]

    # Calculate days since first signal
    if len(events) >= 2:
        span_days = (events[-1].detected_at - events[0].detected_at).days
    else:
        span_days = 0

    sent_any = False
    for addr in alert_emails:
        try:
            send_confluence_alert(
                to_email=addr,
                brand_name=hit.brand_name,
                brand_key=hit.brand_key,
                signal_count=hit.signal_count,
                signal_types=hit.get_signal_types(),
                timeline=timeline,
                span_days=span_days,
                bullish_score=hit.bullish_score,
                watch_level=hit.watch_level,
            )
            sent_any = True
        except Exception as exc:
            logger.warning("Confluence alert email failed to %s: %s", addr, exc)

    if sent_any:
        hit.alert_sent    = True
        hit.alert_sent_at = datetime.now(timezone.utc)
        db.session.commit()

    return sent_any
