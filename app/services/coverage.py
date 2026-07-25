"""
Standing coverage metrics — Task 5.

Two metrics:
  inbox_recall  — % of brands from the deals inbox that the scanner surfaced first
                  (sourced from the latest inbox_audit run)
  lead_time     — for brands the scanner caught before press, days ahead
                  (auto-computed from signal pairs; augmented by manual press-confirm records)
"""
import json
import logging
import statistics
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_PRESS_SIGNAL_TYPES = {"press_stealth", "newswire"}
_EARLY_SIGNAL_TYPES = {"delaware", "trademark", "ctlogs", "domain_ct"}


def _brand_key(name: str) -> str:
    from .confluence import normalize_brand
    return normalize_brand(name or "")


def _as_utc(dt: datetime) -> datetime:
    """
    Normalise a datetime to UTC before any .date() comparison.

    Both branches matter. Naive values are assumed UTC (that's how we write them).
    AWARE values must be CONVERTED, not trusted: the driver hands `created_at` back
    in the server's local zone (e.g. UTC-04:00), and calling .date() on that lands
    on the previous calendar day for timestamps between 00:00 and 04:00 UTC.

    That bug silently inflated lead_time by one day for ~1/6 of all signals, always
    in the flattering direction, and surfaced only as an intermittently failing test
    (2026-07-25). Lead time is the metric this product is judged by — it has to be
    computed in one fixed zone.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def record_press_confirm(brand_name: str, confirmed_at: str, app, source_url: str = "") -> dict:
    """
    Record that a brand appeared in press/newsletter on confirmed_at (ISO date string).
    Upserts one Item(item_type='press_confirm') per brand title.
    """
    from ..models.item import Item
    from ..models.user import User
    from ..extensions import db

    with app.app_context():
        admin = User.query.filter_by(role="admin").first()
        if not admin:
            return {"error": "No admin user found"}

        try:
            datetime.fromisoformat(confirmed_at)
        except ValueError:
            return {"error": f"Invalid confirmed_at: {confirmed_at!r}"}

        desc = json.dumps(
            {"confirmed_at": confirmed_at, "source_url": source_url},
            separators=(",", ":"),
        )
        existing = Item.query.filter_by(
            item_type="press_confirm", owner_id=admin.id, title=brand_name
        ).first()
        if existing:
            existing.description = desc
        else:
            db.session.add(Item(
                title=brand_name,
                item_type="press_confirm",
                owner_id=admin.id,
                description=desc,
            ))
        db.session.commit()
        return {"recorded": True, "brand": brand_name, "confirmed_at": confirmed_at}


def get_coverage_metrics(app, days_back: int = 90) -> dict:
    """
    Compute both coverage metrics.

    Returns:
    {
        "inbox_recall": {
            "coverage_pct":   float,
            "found_count":    int,
            "total_checked":  int,
            "as_of":          str | None,
        },
        "lead_time": {
            "brands":       [{brand, first_signal_date, confirmed_date, days_lead}, ...],
            "median_days":  float | None,
            "mean_days":    float | None,
            "count":        int,
        },
        "computed_at": str,
        "days_back":   int,
    }
    """
    from ..models.item import Item
    from .inbox_audit import get_latest_audit

    with app.app_context():
        # ── 1. Inbox recall from latest audit ────────────────────────────────
        audit = get_latest_audit(app)
        if audit:
            inbox_recall = {
                "coverage_pct":  audit.get("coverage_pct", 0),
                "found_count":   audit.get("found_count", 0),
                "total_checked": audit.get("total_checked", 0),
                "as_of":         audit.get("run_at"),
            }
        else:
            inbox_recall = {
                "coverage_pct": 0,
                "found_count":  0,
                "total_checked": 0,
                "as_of":        None,
            }

        # ── 2. Lead time: auto-detected from signal pairs ─────────────────────
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

        signals = (
            Item.query
            .filter(Item.item_type == "signal", Item.created_at >= cutoff)
            .all()
        )

        early_by_brand: dict[str, datetime] = {}
        press_by_brand: dict[str, datetime] = {}

        for sig in signals:
            try:
                meta = json.loads(sig.description or "{}")
            except Exception:
                continue
            sig_type = meta.get("signal_type", "")
            key = _brand_key(sig.title)
            if not key:
                continue

            dt = _as_utc(sig.created_at)

            if sig_type in _EARLY_SIGNAL_TYPES:
                if key not in early_by_brand or dt < early_by_brand[key]:
                    early_by_brand[key] = dt
            elif sig_type in _PRESS_SIGNAL_TYPES:
                if key not in press_by_brand or dt < press_by_brand[key]:
                    press_by_brand[key] = dt

        # Include manual press-confirm records
        for c in Item.query.filter_by(item_type="press_confirm").all():
            try:
                meta = json.loads(c.description or "{}")
                confirmed_at = _as_utc(datetime.fromisoformat(meta.get("confirmed_at", "")))
                key = _brand_key(c.title)
                if not key:
                    continue
                if key not in press_by_brand or confirmed_at < press_by_brand[key]:
                    press_by_brand[key] = confirmed_at
            except Exception:
                continue

        brands_with_lead = []
        for key, press_dt in press_by_brand.items():
            early_dt = early_by_brand.get(key)
            if not early_dt:
                continue
            early_date = early_dt.date()
            press_date = press_dt.date()
            if early_date < press_date:
                days_lead = (press_date - early_date).days
                brands_with_lead.append({
                    "brand":             key,
                    "first_signal_date": early_date.isoformat(),
                    "confirmed_date":    press_date.isoformat(),
                    "days_lead":         days_lead,
                })

        brands_with_lead.sort(key=lambda x: x["days_lead"], reverse=True)
        day_counts = [b["days_lead"] for b in brands_with_lead]

        return {
            "inbox_recall": inbox_recall,
            "lead_time": {
                "brands":      brands_with_lead,
                "median_days": statistics.median(day_counts) if day_counts else None,
                "mean_days":   round(statistics.mean(day_counts), 1) if day_counts else None,
                "count":       len(brands_with_lead),
            },
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "days_back":   days_back,
        }
