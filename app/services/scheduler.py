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
_scheduler_lock_fd = None  # held open to keep the file lock alive


def _job_lock_key(job_id: str) -> str:
    return f"__jlock_{job_id}__"


def _acquire_job_lock(app, job_id: str, ttl_seconds: int = 3600) -> bool:
    """
    Prevent duplicate job runs across Gunicorn workers.

    Two-layer guard:
    1. pg_try_advisory_xact_lock — truly atomic, non-blocking. Only one PG connection
       can hold the lock at a time, so the first worker wins and all others return False
       immediately without waiting.
    2. TTL row in the items table — persists across restarts so the job isn't re-fired
       if a worker restarts mid-TTL window.

    Committing after updating the TTL row releases the advisory lock (it's xact-scoped),
    so subsequent workers that then acquire the advisory lock will see the updated TTL
    and also return False.
    """
    from ..models.item import Item
    from ..extensions import db
    from sqlalchemy import text

    # Stable bigint key derived from job_id (avoids PYTHONHASHSEED randomness)
    lock_key = int(hashlib.md5(job_id.encode()).hexdigest()[:15], 16) % (2 ** 62)
    lock_title = _job_lock_key(job_id)
    now = datetime.now(timezone.utc)

    with app.app_context():
        try:
            # Atomically try to acquire a transaction-level PG advisory lock.
            # If another worker already holds it, returns False immediately (no blocking).
            acquired = db.session.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": lock_key},
            ).scalar()
            if not acquired:
                logger.info("Job %s: advisory lock held by another worker — skipping", job_id)
                return False

            # TTL check: did any worker run this job recently?
            lock = Item.query.filter_by(title=lock_title, item_type="system").first()
            if lock:
                meta = json.loads(lock.description or "{}")
                ran_at_str = meta.get("ran_at")
                if ran_at_str:
                    ran_at = datetime.fromisoformat(ran_at_str)
                    if (now - ran_at).total_seconds() < ttl_seconds:
                        logger.info(
                            "Job %s: ran %.0fs ago (TTL %ds) — skipping",
                            job_id, (now - ran_at).total_seconds(), ttl_seconds,
                        )
                        return False
                meta["ran_at"] = now.isoformat()
                lock.description = json.dumps(meta)
            else:
                lock = Item(
                    title=lock_title,
                    item_type="system",
                    owner_id=1,
                    description=json.dumps({"ran_at": now.isoformat()}),
                )
                db.session.add(lock)

            # Commit: persists the TTL update AND releases the advisory lock.
            # Workers that subsequently acquire the advisory lock will hit the TTL
            # check above and exit without running the job.
            db.session.commit()
            return True
        except Exception as exc:
            logger.warning("Job lock check failed for %s: %s — running anyway", job_id, exc)
            return True


def _parse_article_date(date_str: str):
    """
    Parse SerpAPI date strings into UTC datetimes for client-side age filtering.
    Handles relative ("2 days ago") and absolute ("May 16, 2025") formats.
    Returns None if the format is unrecognised (caller should not filter those out).
    """
    import re as _re
    if not date_str:
        return None
    s = date_str.strip()
    m = _re.match(r'^(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago$', s.lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        seconds = {
            'second': 1, 'minute': 60, 'hour': 3600, 'day': 86400,
            'week': 604800, 'month': 2592000, 'year': 31536000,
        }[unit]
        return datetime.now(timezone.utc) - timedelta(seconds=n * seconds)
    for fmt in ('%b %d, %Y', '%B %d, %Y', '%b. %d, %Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


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

    if scan_type in ('full', 'producthunt'):
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

    if scan_type in ('full', 'app_store'):
        sources_ran.append('app_store')
        try:
            from ..services.app_store import search_recent_app_store
            as_result = search_recent_app_store(
                days_back=scan.days_back,
                max_results=scan.max_results,
            )
            if not as_result.get("error"):
                signals.extend(as_result["signals"])
            else:
                logger.warning("App Store scan error: %s", as_result["error"])
                errors.append(f"AppStore: {as_result['error']}")
        except Exception as exc:
            logger.warning("App Store import/scan failed: %s", exc)
            errors.append(f"AppStore: {exc}")

    if scan_type in ('full', 'newswire'):
        sources_ran.append('newswire')
        try:
            from ..services.newswire import search_recent_newswire
            nw_result = search_recent_newswire(
                days_back=scan.days_back,
                max_results=scan.max_results,
            )
            if not nw_result.get("error"):
                signals.extend(nw_result["signals"])
            else:
                logger.warning("Newswire scan error: %s", nw_result["error"])
                errors.append(f"Newswire: {nw_result['error']}")
        except Exception as exc:
            logger.warning("Newswire import/scan failed: %s", exc)
            errors.append(f"Newswire: {exc}")

    if scan_type in ('full', 'ctlogs'):
        sources_ran.append('ctlogs')
        try:
            from ..services.ctlogs import search_recent_ct_domains
            ct_result = search_recent_ct_domains(
                days_back=scan.days_back,
                max_results=scan.max_results,
            )
            if not ct_result.get("error"):
                signals.extend(ct_result["signals"])
            else:
                logger.warning("CT logs scan error: %s", ct_result["error"])
                errors.append(f"CTLogs: {ct_result['error']}")
        except Exception as exc:
            logger.warning("CT logs import/scan failed: %s", exc)
            errors.append(f"CTLogs: {exc}")

    if scan_type in ('full', 'press_stealth'):
        sources_ran.append('press_stealth')
        try:
            from ..services.press_stealth import search_recent_press_stealth
            ps_result = search_recent_press_stealth(
                days_back=scan.days_back,
                max_results=scan.max_results,
            )
            if not ps_result.get("error"):
                signals.extend(ps_result["signals"])
            else:
                logger.warning("Press stealth scan error: %s", ps_result["error"])
                errors.append(f"PressStealth: {ps_result['error']}")
        except Exception as exc:
            logger.warning("Press stealth import/scan failed: %s", exc)
            errors.append(f"PressStealth: {exc}")

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
        import re as _re
        _norm = _re.sub(r'\s+', ' ', sig['companyName'].upper().strip())
        key = f"{signal_type}:{_norm}:{sig['timestamp'][:10]}"
        fp  = hashlib.sha256(key.encode()).hexdigest()[:16]

        if fp in existing_fps:
            continue

        item = Item(
            title=sig["companyName"],
            owner_id=user_id,
            item_type="signal",
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

    # ── 3b. Background domain checks for newly saved domain signals ───────────
    for item_id in new_item_ids:
        item = db.session.get(Item, item_id)
        if not item:
            continue
        try:
            _meta = json.loads(item.description or '{}')
            if _meta.get('signal_type') == 'domain' and _meta.get('url'):
                threading.Thread(
                    target=_check_domain_bg,
                    args=(current_app._get_current_object(), item_id, _meta['url']),
                    daemon=True,
                ).start()
        except Exception as exc:
            logger.debug("Domain check launch failed for item %s: %s", item_id, exc)

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
                        _fe_emails_str = os.environ.get("ALERT_EMAILS", "").strip()
                        try:
                            _fe_settings = Item.query.filter_by(title="__bullish_settings__").first()
                            if _fe_settings:
                                _fe_sd = json.loads(_fe_settings.description or "{}")
                                _fe_el = _fe_sd.get("alert_emails", [])
                                if _fe_el:
                                    _fe_emails_str = ",".join(_fe_el)
                        except Exception:
                            pass
                        _fe_alert_emails = [e.strip() for e in _fe_emails_str.split(",") if e.strip()] or None
                        run_founder_enrichment_in_background(
                            current_app._get_current_object(),
                            item_id,
                            meta.get("company_name", item.title),
                            meta.get("category", ""),
                            enrichment.get("one_line_thesis", ""),
                            filer_name=None,
                            alert_emails=_fe_alert_emails,
                        )
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

    # ── 6. Dedup hot_brands (same brand from multiple sources in one run) ────────
    seen_keys: dict = {}
    deduped_hot: list = []
    for b in hot_brands:
        key = b["name"].upper().strip()
        if key not in seen_keys:
            seen_keys[key] = len(deduped_hot)
            deduped_hot.append(b)
        elif (b.get("score") or 0) > (deduped_hot[seen_keys[key]].get("score") or 0):
            deduped_hot[seen_keys[key]] = b
    hot_brands = deduped_hot

    # Per-scan alerts disabled — digest goes out on Mondays via weekly_digest job.
    alert_sent = False
    alert_emails_str = ""

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

    if not _acquire_job_lock(app, "daily_scan", ttl_seconds=82800):
        logger.info("Daily scan: already ran in another worker — skipping")
        return

    # Fire weekly digest on Mondays as a fallback in case the dedicated 9AM job
    # was missed due to a worker restart between deploys.
    if datetime.now(timezone.utc).weekday() == 0:  # 0 = Monday
        try:
            _send_weekly_digest(app)
        except Exception as _exc:
            logger.warning("Weekly digest (fallback) failed: %s", _exc)

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
    if not _acquire_job_lock(app, "weekly_digest", ttl_seconds=82800):  # 23h — one per day max
        logger.info("Weekly digest: already sent by another worker — skipping")
        return
    from ..models.item import Item
    from ..services.email import send_weekly_digest_email
    from datetime import timedelta
    import json

    with app.app_context():
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)

        rows = Item.query.filter(
            Item.item_type == 'signal',
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

        # Deduplicate by brand name: keep the highest-score entry per name
        def _dedup(signals):
            seen: dict = {}
            for s in signals:
                key = (s.get("name") or "").upper().strip()
                if key not in seen or (s.get("score") or 0) > (seen[key].get("score") or 0):
                    seen[key] = s
            return list(seen.values())

        hot_signals  = _dedup(hot_signals)
        warm_signals = _dedup(warm_signals)

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
    if not _acquire_job_lock(app, "founder_news_monitor", ttl_seconds=82800):
        logger.info("Founder news: already ran in another worker — skipping")
        return

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
            Item.item_type == 'watchlist'
        ).all()

        watchlist_items = []
        for row in rows:
            try:
                meta = json.loads(row.description or '{}')
                if meta.get('name'):
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
                # Search for forward-looking news about this founder.
                # Omit the company name (which may be their OLD company) to avoid
                # surfacing retrospective articles. tbs=qdr:2m requests last 2 months
                # from SerpAPI, but the filter is unreliable — we enforce it client-side.
                params = {
                    "engine": "google",
                    "q": f'"{founder_name}" (building OR startup OR raises OR launches OR "new brand" OR "new company" OR "seed round" OR "pre-seed")',
                    "tbm": "nws",
                    "tbs": "qdr:6m",   # hint to SerpAPI — also enforced below
                    "num": 10,
                    "api_key": serpapi_key,
                }
                resp = requests.get("https://serpapi.com/search", params=params, timeout=10)
                if resp.status_code != 200:
                    continue

                data = resp.json()
                new_articles = []
                prev_links = {r.get('link') for r in meta.get('news_results', [])}
                now = datetime.now(timezone.utc)
                cutoff = now - timedelta(days=180)

                for result in data.get('news_results', []):
                    title = result.get('title', '')
                    link  = result.get('link', '')
                    snippet = result.get('snippet', '')
                    date_str = result.get('date', '')

                    if link in prev_links:
                        continue

                    # Client-side date enforcement — SerpAPI's tbs filter is unreliable
                    # and can return articles from years ago (e.g. 2016 photo credits,
                    # obituaries for people with the same name). Rolling 6-month window.
                    article_date = _parse_article_date(date_str)
                    if article_date is not None and article_date < cutoff:
                        continue

                    new_articles.append({
                        'title': title,
                        'link': link,
                        'snippet': snippet,
                        'date': date_str,
                        'source': result.get('source', ''),
                    })

                # Cap per-founder sends — especially important on first run when
                # prev_links is empty and all results are technically "new".
                new_articles = new_articles[:5]

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


def _check_domain_bg(app, item_id: int, url: str) -> None:
    """Background thread — crawl a domain URL and persist status to item metadata."""
    try:
        from .domain_checker import check_domain_status
        status = check_domain_status(url)
    except Exception as exc:
        logger.warning("Domain checker failed for item %s: %s", item_id, exc)
        return

    try:
        from ..models.item import Item as _Item
        from ..extensions import db as _db
        with app.app_context():
            item = _db.session.get(_Item, item_id)
            if not item:
                return
            meta = json.loads(item.description or '{}')
            meta['domain_status'] = status
            item.description = json.dumps(meta, separators=(',', ':'))
            _db.session.commit()
            logger.info("Domain status for item %s: %s", item_id, status.get('status'))
    except Exception as exc:
        logger.warning("Domain status DB write failed for item %s: %s", item_id, exc)


def _run_press_monitor(app):
    """
    Weekly job (Thu 08:00 UTC): scan ~20 consumer trade press RSS feeds and
    cross-reference against every brand in the signal DB.
    Appends new press mentions to each brand's item metadata without duplicates.
    """
    if not _acquire_job_lock(app, "press_monitor", ttl_seconds=82800):
        logger.info("Press monitor: already ran in another worker — skipping")
        return
    from ..models.item import Item
    from ..extensions import db

    with app.app_context():
        items = Item.query.filter_by(item_type='signal').all()

        brand_map: dict[str, object] = {}
        for item in items:
            try:
                meta = json.loads(item.description or '{}')
                name = (meta.get('company_name') or item.title or '').strip()
                if len(name) >= 4:
                    brand_map[name.upper()] = item
            except Exception:
                pass

        if not brand_map:
            logger.info("Press monitor: no brands in DB — skipping")
            return

        logger.info("Press monitor: checking %d brands", len(brand_map))

        try:
            from .press_monitor import scan_press_for_brands
            mentions = scan_press_for_brands(list(brand_map.keys()))
        except Exception as exc:
            logger.warning("Press monitor scan failed: %s", exc)
            return

        updated = 0
        for brand_key, articles in mentions.items():
            item = brand_map.get(brand_key)
            if not item or not articles:
                continue
            try:
                meta = json.loads(item.description or '{}')
                existing_urls = {a.get('url') for a in meta.get('press_mentions', [])}
                new_only = [a for a in articles if a.get('url') not in existing_urls]
                meta['press_mentions'] = (new_only + meta.get('press_mentions', []))[:20]
                meta['press_checked_at'] = datetime.now(timezone.utc).isoformat()
                item.description = json.dumps(meta, separators=(',', ':'))
                updated += 1
            except Exception as exc:
                logger.warning("Press mention update failed for %s: %s", brand_key, exc)

        db.session.commit()
        logger.info("Press monitor: updated %d brands with new press mentions", updated)


def _log_inbox_audit_reminder(app):
    """
    Monthly job (1st of month, 07:00 UTC): log a reminder that the Gmail
    Deals inbox audit is due. The actual audit is triggered by the Claude
    Code scheduled task 'Stealth Finder Monthly Inbox Audit' which reads
    the INVESTMENTS/A. Deals Gmail label via MCP and POSTs brand names to
    POST /api/admin/inbox-audit/run.
    """
    with app.app_context():
        logger.info(
            "Monthly inbox audit reminder — the Claude Code scheduled task "
            "should be POSTing deal brand names to /api/admin/inbox-audit/run. "
            "Check Settings → Tools → Inbox Audit for the last result."
        )


def start_scheduler(app):
    """Start the APScheduler background scheduler (once per process).

    Gunicorn forks N worker processes; each would start its own scheduler and
    fire every job N times. We use an exclusive non-blocking fcntl file lock
    so only ONE worker wins and runs the scheduler — the others exit silently.
    The lock is kept alive by holding the open file descriptor in
    _scheduler_lock_fd for the lifetime of the process.
    """
    global _scheduler, _scheduler_lock_fd
    if _scheduler is not None:
        return  # Already running in this process

    # ── File lock: only one worker gets to run the scheduler ─────────────────
    import fcntl, tempfile
    lock_path = os.path.join(tempfile.gettempdir(), "bullish_scheduler.lock")
    try:
        fd = open(lock_path, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # non-blocking exclusive
        _scheduler_lock_fd = fd   # keep open — lock released only when fd closes
        logger.info("Scheduler file lock acquired — this worker owns the scheduler")
    except (IOError, OSError):
        logger.info("Scheduler: another worker holds the lock — skipping scheduler start")
        return
    # ─────────────────────────────────────────────────────────────────────────

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
        _scheduler.add_job(
            _run_press_monitor,
            trigger=CronTrigger(day_of_week="thu", hour=8, minute=0, timezone="UTC"),
            args=[app],
            id="press_monitor",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        _scheduler.add_job(
            _log_inbox_audit_reminder,
            trigger=CronTrigger(day=1, hour=7, minute=0, timezone="UTC"),
            args=[app],
            id="monthly_inbox_audit_reminder",
            replace_existing=True,
            misfire_grace_time=86400,
        )
        _scheduler.start()
        logger.info(
            "Bullish scheduler started — daily scan 06:00 UTC, weekly digest Mon 09:00 UTC, "
            "founder news Wed 08:00 UTC, press monitor Thu 08:00 UTC, "
            "inbox audit reminder 1st of month 07:00 UTC"
        )
    except Exception as exc:
        logger.warning("Scheduler could not start: %s", exc)
