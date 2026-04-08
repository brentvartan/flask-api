"""Auto-watchlist service — adds HOT brands to watchlist automatically."""
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_SETTINGS_TITLE = "__bullish_settings__"
_YEAR_PREFIX_RE = re.compile(r'^\d{4}\s*(Theme:?\s*)?', re.IGNORECASE)


def _strip_year(theme: str) -> str:
    return _YEAR_PREFIX_RE.sub('', theme).strip()


def _get_watchlist_items(owner_id: int):
    from ..extensions import db
    from ..models.item import Item
    return Item.query.filter(
        Item.owner_id == owner_id,
        Item.item_type == 'watchlist',
    ).all()


def _find_watchlist_entry(items, brand_key: str):
    """Return (item, meta) for a matching brand, or (None, None)."""
    for item in items:
        try:
            meta = json.loads(item.description or '{}')
            if (meta.get('company') or item.title or '').upper().strip() == brand_key:
                return item, meta
        except Exception:
            pass
    return None, None


def auto_add_to_watchlist(
    app_context,
    owner_id: int,
    brand_name: str,
    signal_item_id: int,
    bullish_score: int,
    founder_name: str | None,
    founder_score: int | None,
    linkedin_url: str | None,
    thesis: str | None,
    cultural_theme: str | None,
    signal_types: list,
) -> bool:
    """
    Add a HOT brand to the watchlist if not already present.
    Returns True if a new entry was created, False if already exists.
    """
    from ..extensions import db
    from ..models.item import Item

    with app_context:
        try:
            brand_key = brand_name.upper().strip()
            existing_items = _get_watchlist_items(owner_id)
            wl_item, wl_meta = _find_watchlist_entry(existing_items, brand_key)

            if wl_item:
                # Already on watchlist — merge any new signal types
                existing_types = set(wl_meta.get('signal_types') or [])
                new_types = existing_types | set(signal_types)
                if new_types != existing_types:
                    wl_meta['signal_types'] = list(new_types)
                    wl_item.description = json.dumps(wl_meta)
                    db.session.commit()
                return False

            theme_clean = _strip_year(cultural_theme or '')
            notes = f"Auto-added · HOT {bullish_score}"
            if theme_clean:
                notes += f" · {theme_clean}"

            meta = {
                "_type":           "watchlist",
                "name":            founder_name or "",
                "company":         brand_name,
                "linkedin":        linkedin_url or "",
                "twitter":         "",
                "notes":           notes,
                "added_at":        datetime.now(timezone.utc).isoformat(),
                "auto_added":      True,
                "signal_item_id":  signal_item_id,
                "bullish_score":   bullish_score,
                "founder_score":   founder_score,
                "signal_types":    signal_types,
                "cultural_theme":  theme_clean,
                "thesis":          thesis or "",
                "last_news_check": None,
                "news_results":    [],
                "rescore_history": [],
            }

            db.session.add(Item(
                title=brand_name,
                owner_id=owner_id,
                item_type="watchlist",
                description=json.dumps(meta),
            ))
            db.session.commit()
            logger.info("Auto-added %s to watchlist (score %d)", brand_name, bullish_score)
            return True

        except Exception as exc:
            logger.warning("auto_add_to_watchlist failed for %s: %s", brand_name, exc)
            return False


def trigger_rescore_if_watchlisted(app_context, brand_name: str, new_signal_type: str, owner_id: int):
    """
    If brand is on watchlist, re-enrich its signal item with updated confluence context.
    Fires an alert if score jumps >=5 points.
    """
    def _run():
        from ..extensions import db
        from ..models.item import Item

        with app_context:
            try:
                brand_key = brand_name.upper().strip()
                wl_item, wl_meta = _find_watchlist_entry(_get_watchlist_items(owner_id), brand_key)

                if not wl_item:
                    return

                signal_item_id = wl_meta.get('signal_item_id')
                if not signal_item_id:
                    return

                signal_item = db.session.get(Item, signal_item_id)
                if not signal_item:
                    return

                signal_meta = json.loads(signal_item.description or '{}')
                old_score = (signal_meta.get('enrichment') or {}).get('bullish_score') or 0
                existing_types = list(set(wl_meta.get('signal_types', []) + [new_signal_type]))

                from ..services.enrichment import enrich_signal
                enrichment = enrich_signal({
                    "companyName":  signal_meta.get("company_name", signal_item.title),
                    "category":     signal_meta.get("category", ""),
                    "signal_type":  signal_meta.get("signal_type", "trademark"),
                    "description":  signal_meta.get("description", ""),
                    "notes":        signal_meta.get("notes", ""),
                    "signal_count": len(existing_types),
                    "signal_types": existing_types,
                })

                if not enrichment.get("enriched"):
                    return

                new_score = enrichment.get("bullish_score") or 0
                score_delta = new_score - old_score

                signal_meta["enrichment"] = enrichment
                signal_item.description = json.dumps(signal_meta, separators=(",", ":"))

                wl_meta["bullish_score"]  = new_score
                wl_meta["signal_types"]   = existing_types
                wl_meta["rescore_history"] = (wl_meta.get("rescore_history") or []) + [{
                    "date":      datetime.now(timezone.utc).isoformat(),
                    "old_score": old_score,
                    "new_score": new_score,
                    "trigger":   new_signal_type,
                }]
                wl_item.description = json.dumps(wl_meta)
                db.session.commit()

                logger.info("Re-scored %s: %d -> %d (trigger: %s)", brand_name, old_score, new_score, new_signal_type)

                if score_delta >= 5:
                    _send_rescore_alert(brand_name, old_score, new_score, new_signal_type, existing_types, enrichment)

            except Exception as exc:
                logger.warning("trigger_rescore_if_watchlisted failed for %s: %s", brand_name, exc)

    threading.Thread(target=_run, daemon=True).start()


def _send_rescore_alert(brand_name, old_score, new_score, new_signal_type, signal_types, enrichment):
    """Send rescore alert emails to configured recipients."""
    from ..models.item import Item
    from ..services.email import send_rescore_alert

    alert_emails_str = os.environ.get("ALERT_EMAILS", "").strip()
    try:
        settings = Item.query.filter_by(title=_SETTINGS_TITLE).first()
        if settings:
            s = json.loads(settings.description or '{}')
            if s.get('alert_emails'):
                alert_emails_str = ",".join(s['alert_emails'])
    except Exception:
        pass

    for addr in [e.strip() for e in alert_emails_str.split(",") if e.strip()]:
        try:
            send_rescore_alert(
                addr,
                brand_name=brand_name,
                old_score=old_score,
                new_score=new_score,
                new_signal_type=new_signal_type,
                signal_types=signal_types,
                thesis=enrichment.get("one_line_thesis", ""),
            )
        except Exception as exc:
            logger.warning("Rescore alert failed: %s", exc)
