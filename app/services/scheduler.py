"""
Bullish Stealth Finder — Background Scheduler Service.

Runs daily USPTO scans, enriches new signals with Bullish AI,
and fires HOT-signal email alerts to the team.
"""
import os
import json
import hashlib
import logging
import threading

from datetime import datetime, timezone, timedelta
from flask import current_app

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

    # ── 1. Fetch signals based on scan_type ───────────────────────────────────
    from ..services.delaware import search_recent_delaware_entities

    signals = []
    scan_type = getattr(scan, 'scan_type', 'full') or 'full'
    sources_ran = []
    errors = []

    if scan_type in ('full', 'trademark'):
        sources_ran.append('trademark')
        tm_result = search_recent_trademarks(
            days_back=scan.days_back,
            max_results=scan.max_results,
        )
        if not tm_result.get("error"):
            signals.extend(tm_result["signals"])
        else:
            logger.warning("USPTO scan error: %s", tm_result["error"])
            errors.append(f"USPTO: {tm_result['error']}")

    if scan_type in ('full', 'delaware'):
        sources_ran.append('delaware')
        de_result = search_recent_delaware_entities(
            days_back=scan.days_back,
            max_results=150,
            check_domains=True,
        )
        if not de_result.get("error"):
            signals.extend(de_result["signals"])
        else:
            logger.warning("Delaware scan error: %s", de_result["error"])
            errors.append(f"Delaware: {de_result['error']}")

    if scan_type == 'producthunt':
        sources_ran.append('producthunt')
        try:
            from ..services.producthunt import search_recent_producthunt
            ph_result = search_recent_producthunt(
                days_back=scan.days_back,
                max_results=scan.max_results,
            )
            if not ph_result.get("error"):
                signals.extend(ph_result["signals"])
            else:
                logger.warning("Product Hunt scan error: %s", ph_result["error"])
                errors.append(f"ProductHunt: {ph_result['error']}")
        except Exception as exc:
            logger.warning("Product Hunt import/scan failed: %s", exc)
            errors.append(f"ProductHunt: {exc}")

    sources_ran_str = ",".join(sources_ran)

    if not signals:
        error_msg = "; ".join(errors) if errors else "All signal sources failed"
        return {"error": "All signal sources failed", "new_saved": 0, "hot_found": 0, "sources_ran": sources_ran_str, "error_message": error_msg}

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
    hot_count  = 0
    warm_count = 0
    cold_count = 0
    founders_queued = 0

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
                level = enrichment.get("watch_level")
                if level == "hot":
                    hot_count += 1
                    hot_brands.append({
                        "name":     meta.get("company_name", item.title),
                        "category": meta.get("category", ""),
                        "score":    enrichment.get("bullish_score"),
                        "thesis":   enrichment.get("one_line_thesis", ""),
                        "theme":    enrichment.get("cultural_theme", ""),
                        "item_id":  item_id,
                    })
                    # Trigger founder enrichment in background for HOT brands
                    try:
                        from ..services.founder_enrichment import run_founder_enrichment_in_background
                        run_founder_enrichment_in_background(item_id)
                        founders_queued += 1
                    except Exception as fe:
                        logger.warning("Founder enrichment trigger failed for item %s: %s", item_id, fe)
                elif level == "warm":
                    warm_count += 1
                else:
                    cold_count += 1
        except Exception as exc:
            logger.warning("Enrichment failed for item %s: %s", item_id, exc)

    if new_item_ids:
        db.session.commit()

    # ── 5. Confluence detection for newly saved signals ───────────────────────
    try:
        from ..services.confluence import record_signal_and_check_confluence, send_confluence_alert_for_hit

        confluence_alert_emails_str = os.environ.get("ALERT_EMAILS", "").strip()
        try:
            _cse_item = Item.query.filter_by(title="__bullish_settings__").first()
            if _cse_item:
                _cse_settings = json.loads(_cse_item.description or "{}")
                _cse_emails = _cse_settings.get("alert_emails", [])
                if _cse_emails:
                    confluence_alert_emails_str = ",".join(_cse_emails)
        except Exception:
            pass
        confluence_alert_emails = [e.strip() for e in confluence_alert_emails_str.split(",") if e.strip()]

        for item_id in new_item_ids:
            item = db.session.get(Item, item_id)
            if not item:
                continue
            try:
                meta = json.loads(item.description or "{}")
                enrichment = meta.get("enrichment") or {}
                result = record_signal_and_check_confluence(
                    item_id=item_id,
                    owner_id=user_id,
                    brand_name=meta.get("company_name", item.title),
                    signal_type=meta.get("signal_type", "trademark"),
                    source_url=meta.get("url"),
                    enrichment=enrichment if enrichment.get("enriched") else None,
                )
                if result["is_confluence"] and result.get("hit_id") and confluence_alert_emails:
                    send_confluence_alert_for_hit(result["hit_id"], confluence_alert_emails)
                    logger.info("Confluence alert sent for %s", item.title)
            except Exception as exc:
                logger.warning("Confluence check failed for item %s: %s", item_id, exc)
    except Exception as exc:
        logger.warning("Confluence detection block failed: %s", exc)

    # ── 5b. Auto-add HOT brands to watchlist ─────────────────────────────────
    try:
        from ..services.watchlist import auto_add_to_watchlist
        for brand in hot_brands:
            item = db.session.get(Item, brand["item_id"])
            if not item:
                continue
            meta = json.loads(item.description or "{}")
            enrichment = meta.get("enrichment", {})
            founder = enrichment.get("founder", {})
            _sig_type = meta.get("signal_type", "trademark")
            _sig_types = [_sig_type] if isinstance(_sig_type, str) else [_sig_type]
            threading.Thread(
                target=auto_add_to_watchlist,
                args=(
                    current_app._get_current_object().app_context(),
                    user_id,
                    brand["name"],
                    brand["item_id"],
                    brand["score"],
                    founder.get("name") if founder.get("confidence") != "unknown" else None,
                    founder.get("founder_score"),
                    founder.get("linkedin_url"),
                    brand["thesis"],
                    brand["theme"],
                    _sig_types,
                ),
                daemon=True,
            ).start()
    except Exception as exc:
        logger.warning("Auto-watchlist trigger failed: %s", exc)

    # ── 6. Send HOT alert email + Slack ──────────────────────────────────────
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

    # ── Dedup: within this run, same brand may appear via trademark + Delaware ───
    # Keep the highest-scored entry per brand name so one brand = one email card.
    seen_keys: dict = {}
    deduped_hot: list = []
    for b in hot_brands:
        key = b["name"].upper().strip()
        if key not in seen_keys:
            seen_keys[key] = len(deduped_hot)
            deduped_hot.append(b)
        elif (b.get("score") or 0) > (deduped_hot[seen_keys[key]].get("score") or 0):
            deduped_hot[seen_keys[key]] = b  # replace with higher-scored version
    hot_brands = deduped_hot

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

    # ── 7. Update scan record ─────────────────────────────────────────────────
    scan.last_run_at   = datetime.now(timezone.utc)
    scan.last_run_new  = new_saved
    scan.last_run_hot  = hot_count
    scan.last_run_warm = warm_count
    scan.last_run_cold = cold_count
    scan.total_signals        = (scan.total_signals or 0) + new_saved
    scan.total_hot            = (scan.total_hot or 0) + hot_count
    scan.total_warm           = (scan.total_warm or 0) + warm_count
    scan.last_alert_sent      = alert_sent
    scan.last_alert_emails    = alert_emails_str if alert_sent else None
    scan.last_founders_queued = founders_queued

    # ── 8. Persist ScanRun history record ────────────────────────────────────
    from ..models.scan_run import ScanRun
    run = ScanRun(
        scan_id=scan.id,
        owner_id=user_id,
        ran_at=datetime.now(timezone.utc),
        new_saved=new_saved,
        hot_found=hot_count,
        warm_found=warm_count,
        cold_found=cold_count,
        founders_queued=founders_queued,
        alert_sent=alert_sent,
        alert_emails=alert_emails_str if alert_sent else None,
        sources_ran=sources_ran_str,
        error_message="; ".join(errors) if errors else None,
    )
    db.session.add(run)
    db.session.commit()

    return {
        "new_saved":      new_saved,
        "hot_found":      len(hot_brands),
        "warm_found":     warm_count,
        "cold_found":     cold_count,
        "total_fetched":  len(signals),
        "alert_sent":     alert_sent,
        "founders_queued": founders_queued,
        "sources_ran":    sources_ran_str,
        "error_message":  "; ".join(errors) if errors else None,
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


def _check_founder_news(app):
    """
    Weekly job: for every watchlist entry with a founder name,
    run a SerpAPI news search and email alerts on new results.
    """
    import os, json, requests
    from ..models.item import Item
    from ..services.email import send_founder_news_alert
    from ..extensions import db
    from datetime import datetime, timezone, timedelta

    serpapi_key = os.environ.get("SERPAPI_API_KEY", "")
    if not serpapi_key:
        return

    with app.app_context():
        # Load all watchlist items with a founder name
        rows = Item.query.filter(
            Item.description.contains('"_type"')
        ).all()

        watchlist_items = []
        for row in rows:
            try:
                meta = json.loads(row.description or '{}')
                if meta.get('_type') == 'watchlist' and meta.get('name'):
                    watchlist_items.append((row, meta))
            except Exception:
                pass

        if not watchlist_items:
            return

        alert_emails = os.environ.get("ALERT_EMAILS", "").strip()
        try:
            settings = Item.query.filter_by(title="__bullish_settings__").first()
            if settings:
                s = json.loads(settings.description or '{}')
                if s.get('alert_emails'):
                    alert_emails = ",".join(s['alert_emails'])
        except Exception:
            pass

        for row, meta in watchlist_items:
            founder_name = meta.get('name', '').strip()
            company = meta.get('company', '').strip()
            if not founder_name or not company:
                continue

            try:
                # SerpAPI news search
                params = {
                    "engine": "google",
                    "q": f'"{founder_name}" "{company}"',
                    "tbm": "nws",
                    "num": 5,
                    "api_key": serpapi_key,
                }
                resp = requests.get("https://serpapi.com/search", params=params, timeout=10)
                if resp.status_code != 200:
                    continue

                data = resp.json()
                new_articles = []

                for result in data.get('news_results', []):
                    title = result.get('title', '')
                    link  = result.get('link', '')
                    snippet = result.get('snippet', '')
                    date_str = result.get('date', '')

                    # Check if this link was in previous results
                    prev_links = {r.get('link') for r in meta.get('news_results', [])}
                    if link not in prev_links:
                        new_articles.append({
                            'title': title,
                            'link': link,
                            'snippet': snippet,
                            'date': date_str,
                            'source': result.get('source', ''),
                        })

                # Update stored results (keep last 10)
                all_results = new_articles + (meta.get('news_results') or [])
                meta['news_results'] = all_results[:10]
                meta['last_news_check'] = datetime.now(timezone.utc).isoformat()
                row.description = json.dumps(meta)
                db.session.commit()

                # Send alert if new articles found
                if new_articles and alert_emails:
                    for addr in [e.strip() for e in alert_emails.split(',') if e.strip()]:
                        try:
                            send_founder_news_alert(
                                addr,
                                founder_name=founder_name,
                                company=company,
                                bullish_score=meta.get('bullish_score'),
                                new_articles=new_articles,
                                linkedin_url=meta.get('linkedin', ''),
                            )
                        except Exception as exc:
                            logger.warning("Founder news alert failed to %s: %s", addr, exc)

            except Exception as exc:
                logger.warning("Founder news check failed for %s / %s: %s", founder_name, company, exc)


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
        _scheduler.add_job(
            _check_founder_news,
            trigger=CronTrigger(day_of_week="wed", hour=8, minute=0, timezone="UTC"),
            args=[app],
            id="founder_news_monitor",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        _scheduler.start()
        logger.info("Bullish scheduler started — daily scan 06:00 UTC, weekly digest Mon 09:00 UTC, founder news Wed 08:00 UTC")
    except Exception as exc:
        logger.warning("Scheduler could not start: %s", exc)
