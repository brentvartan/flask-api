"""
Bullish Stealth Finder — Background Scheduler Service.

Runs daily USPTO scans, enriches new signals with Bullish AI,
and fires HOT-signal email alerts to the team.
"""
import os
import json
import hashlib
import logging

from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Module-level flag so only ONE scheduler starts per process
_scheduler = None


# ─── Core: run a single scan now ──────────────────────────────────────────────

def run_scan_now(scan, user_id: int) -> dict:
    """
    Execute a ScheduledScan immediately:
      1. Fetch new USPTO trademark filings
      2. Save new signals (deduplicated)
      3. Enrich with Bullish AI
      4. Email HOT-signal alert if any found

    Returns a result dict suitable for the API response.
    """
    from ..models.item import Item
    from ..services.trademarks import search_recent_trademarks
    from ..services.enrichment import enrich_signal
    from ..services.email import send_hot_alert
    from ..extensions import db

    # ── 1. Fetch signals from all live sources ────────────────────────────────
    from ..services.delaware import search_recent_delaware_entities

    signals = []

    tm_result = search_recent_trademarks(
        days_back=scan.days_back,
        max_results=scan.max_results,
    )
    if not tm_result.get("error"):
        signals.extend(tm_result["signals"])
    else:
        logger.warning("USPTO scan error: %s", tm_result["error"])

    de_result = search_recent_delaware_entities(
        days_back=scan.days_back,
        max_results=150,
        check_domains=True,
    )
    if not de_result.get("error"):
        signals.extend(de_result["signals"])
    else:
        logger.warning("Delaware scan error: %s", de_result["error"])

    if not signals:
        return {"error": "All signal sources failed", "new_saved": 0, "hot_found": 0}

    # ── 2. Load existing fingerprints (dedup) ─────────────────────────────────
    rows = (
        Item.query
        .filter(Item.description.contains('"fp"'))
        .with_entities(Item.description)
        .all()
    )
    existing_fps = set()
    for (desc,) in rows:
        try:
            fp = json.loads(desc or "{}").get("fp")
            if fp:
                existing_fps.add(fp)
        except Exception:
            pass

    # ── 3. Persist new signals ────────────────────────────────────────────────
    new_saved = 0
    new_item_ids = []

    for sig in signals:
        signal_type = sig.get("signal_type", "trademark")
        key = f"{signal_type}:{sig['companyName'].upper().strip()}:{sig['timestamp'][:10]}"
        fp  = hashlib.sha256(key.encode()).hexdigest()[:16]

        if fp in existing_fps:
            continue

        item = Item(
            title=sig["companyName"],
            owner_id=user_id,
            description=json.dumps({
                "_type":        "signal",
                "fp":           fp,
                "company_name": sig["companyName"],
                "signal_type":  signal_type,
                "category":     sig["category"],
                "score_boost":  sig.get("score_boost", 5),
                "description":  sig["description"],
                "url":          sig["url"],
                "notes":        sig.get("notes", ""),
                "timestamp":    sig["timestamp"],
            }, separators=(",", ":")),
        )
        db.session.add(item)
        db.session.flush()          # get item.id before commit
        new_item_ids.append(item.id)
        existing_fps.add(fp)
        new_saved += 1

    if new_saved > 0:
        db.session.commit()

    # ── 4. Enrich new signals with Bullish AI ─────────────────────────────────
    hot_brands = []

    for item_id in new_item_ids:
        item = db.session.get(Item, item_id)
        if not item:
            continue
        try:
            meta = json.loads(item.description or "{}")
            enrichment = enrich_signal({
                "companyName": meta.get("company_name", item.title),
                "category":    meta.get("category", ""),
                "signal_type": "trademark",
                "description": meta.get("description", ""),
                "notes":       meta.get("notes", ""),
            })
            if enrichment.get("enriched"):
                meta["enrichment"] = enrichment
                item.description = json.dumps(meta, separators=(",", ":"))
                if enrichment.get("watch_level") == "hot":
                    hot_brands.append({
                        "name":     meta.get("company_name", item.title),
                        "category": meta.get("category", ""),
                        "score":    enrichment.get("bullish_score"),
                        "thesis":   enrichment.get("one_line_thesis", ""),
                        "theme":    enrichment.get("cultural_theme", ""),
                        "item_id":  item_id,
                    })
        except Exception as exc:
            logger.warning("Enrichment failed for item %s: %s", item_id, exc)

    if new_item_ids:
        db.session.commit()

    # ── 5. Send HOT alert email + Slack ──────────────────────────────────────
    from ..services.slack import send_slack_hot_alert

    # Read alert_emails from DB settings first, fall back to env var
    alert_emails_str = os.environ.get("ALERT_EMAILS", "").strip()
    try:
        from ..models.item import Item as _Item
        _settings_item = _Item.query.filter_by(title="__bullish_settings__").first()
        if _settings_item:
            _settings = json.loads(_settings_item.description or "{}")
            _emails_list = _settings.get("alert_emails", [])
            if _emails_list:
                alert_emails_str = ",".join(_emails_list)
    except Exception:
        pass

    alert_sent = False

    if hot_brands and alert_emails_str:
        for addr in [e.strip() for e in alert_emails_str.split(",") if e.strip()]:
            try:
                send_hot_alert(addr, hot_brands, scan.name)
                alert_sent = True
            except Exception as exc:
                logger.warning("HOT alert email failed to %s: %s", addr, exc)

    if hot_brands:
        try:
            send_slack_hot_alert(hot_brands, scan.name)
        except Exception as exc:
            logger.warning("Slack HOT alert failed: %s", exc)

    # ── 6. Update scan record ─────────────────────────────────────────────────
    scan.last_run_at  = datetime.now(timezone.utc)
    scan.last_run_new = new_saved
    db.session.commit()

    return {
        "new_saved":     new_saved,
        "hot_found":     len(hot_brands),
        "total_fetched": len(signals),
        "alert_sent":    alert_sent,
    }


# ─── APScheduler daily job ────────────────────────────────────────────────────

def _run_all_scheduled(app):
    """APScheduler job — executes every enabled scan for every user."""
    from ..models.scheduled_scan import ScheduledScan
    from datetime import timedelta

    with app.app_context():
        scans = ScheduledScan.query.filter_by(enabled=True).all()
        for scan in scans:
            # Skip if already run within the cooldown window
            if scan.last_run_at:
                cooldown_hours = 20 if scan.frequency == "daily" else 140
                age = datetime.now(timezone.utc) - scan.last_run_at
                if age < timedelta(hours=cooldown_hours):
                    logger.info("Skipping scan %s — ran %s ago", scan.id, age)
                    continue
            try:
                logger.info("Running scheduled scan %s for user %s", scan.id, scan.owner_id)
                run_scan_now(scan, scan.owner_id)
            except Exception as exc:
                logger.error("Scheduled scan %s failed: %s", scan.id, exc)


def _send_weekly_digest(app):
    """APScheduler job — every Monday 9:00 UTC. Sends top HOT/WARM signals from the past 7 days."""
    from ..models.item import Item
    from ..services.email import send_weekly_digest_email
    from datetime import timedelta
    import json

    with app.app_context():
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)

        rows = Item.query.filter(
            Item.description.contains('"enrichment"'),
            Item.created_at >= week_ago,
        ).all()

        hot_signals  = []
        warm_signals = []

        for item in rows:
            try:
                meta = json.loads(item.description or "{}")
                enrichment = meta.get("enrichment", {})
                if not enrichment.get("enriched"):
                    continue
                watch_level = enrichment.get("watch_level")
                if watch_level not in ("hot", "warm"):
                    continue
                entry = {
                    "name":     meta.get("company_name", item.title),
                    "category": meta.get("category", ""),
                    "score":    enrichment.get("bullish_score"),
                    "thesis":   enrichment.get("one_line_thesis", ""),
                    "theme":    enrichment.get("cultural_theme", ""),
                }
                if watch_level == "hot":
                    hot_signals.append(entry)
                else:
                    warm_signals.append(entry)
            except Exception:
                pass

        if not hot_signals and not warm_signals:
            logger.info("Weekly digest: no HOT/WARM signals this week — skipping send")
            return

        hot_signals.sort(key=lambda x: x.get("score") or 0, reverse=True)
        warm_signals.sort(key=lambda x: x.get("score") or 0, reverse=True)

        alert_emails = os.environ.get("ALERT_EMAILS", "").strip()
        try:
            from ..models.item import Item as _Item2
            _s2 = _Item2.query.filter_by(title="__bullish_settings__").first()
            if _s2:
                _sd = json.loads(_s2.description or "{}")
                _el = _sd.get("alert_emails", [])
                if _el:
                    alert_emails = ",".join(_el)
        except Exception:
            pass
        if not alert_emails:
            logger.info("Weekly digest: ALERT_EMAILS not set — skipping send")
            return

        week_label = datetime.now(timezone.utc).strftime("%b %d, %Y")
        for addr in [e.strip() for e in alert_emails.split(",") if e.strip()]:
            try:
                send_weekly_digest_email(addr, hot_signals[:5], warm_signals[:5], week_label)
                logger.info("Weekly digest sent to %s", addr)
            except Exception as exc:
                logger.warning("Weekly digest email failed to %s: %s", addr, exc)


def start_scheduler(app):
    """Start the APScheduler background scheduler (once per process)."""
    global _scheduler
    if _scheduler is not None:
        return  # Already running in this process

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        _scheduler = BackgroundScheduler(daemon=True)
        _scheduler.add_job(
            _run_all_scheduled,
            trigger=CronTrigger(hour=6, minute=0, timezone="UTC"),
            args=[app],
            id="daily_bullish_scan",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        _scheduler.add_job(
            _send_weekly_digest,
            trigger=CronTrigger(day_of_week="mon", hour=9, minute=0, timezone="UTC"),
            args=[app],
            id="weekly_digest",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        _scheduler.start()
        logger.info("Bullish scheduler started — daily scan 06:00 UTC, weekly digest Mon 09:00 UTC")
    except Exception as exc:
        logger.warning("Scheduler could not start: %s", exc)
