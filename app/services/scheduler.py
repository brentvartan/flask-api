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


# ─── System-row ownership ─────────────────────────────────────────────────────

def _system_owner_id():
    """
    Resolve a REAL users.id to own scheduler bookkeeping rows (heartbeat,
    job-lock TTL).

    Item.owner_id is a NOT NULL FK to users.id. These rows used to hardcode
    owner_id=1, and production has no user 1 — so every insert raised an
    IntegrityError that the surrounding broad except swallowed, permanently
    zeroing the heartbeat and the lock TTL row.

    Prefers the lowest-id admin, falls back to the lowest-id user, and returns
    None when the users table is empty (callers must skip the write, not raise).
    Deliberately NOT cached: a user deleted later must not poison the process.

    Must be called inside an app context.
    """
    from ..models.user import User
    row = (
        User.query.filter_by(role="admin").order_by(User.id.asc()).first()
        or User.query.order_by(User.id.asc()).first()
    )
    return row.id if row else None


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

    Fails CLOSED: any error resolving the lock returns False (skip the run). The
    old "run anyway" fallback meant a persistent lock error fired the job in every
    worker at once.
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
                owner_id = _system_owner_id()
                if owner_id is None:
                    logger.error(
                        "Job %s: no user row to own the lock — skipping run", job_id
                    )
                    return False
                lock = Item(
                    title=lock_title,
                    item_type="system",
                    owner_id=owner_id,
                    description=json.dumps({"ran_at": now.isoformat()}),
                )
                db.session.add(lock)

            # Commit: persists the TTL update AND releases the advisory lock.
            # Workers that subsequently acquire the advisory lock will hit the TTL
            # check above and exit without running the job.
            db.session.commit()
            return True
        except Exception as exc:
            # Fail CLOSED. Every Gunicorn worker can end up running this check,
            # so answering "you hold the lock" on a persistent error means N
            # workers run the same job and pay for enrichment N times. A skipped
            # run is far cheaper than a multiplied one — and the next scheduled
            # run (or a manual trigger) recovers it.
            logger.error("Job lock check failed for %s: %s — skipping run", job_id, exc)
            return False


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

def run_scan_now(scan, user_id: int, days_back_override: int = None) -> dict:
    """
    Execute a ScheduledScan immediately:
      1. Fetch new USPTO trademark filings
      2. Save new signals (deduplicated)
      3. Enrich with Bullish AI
      4. Email HOT-signal alert if any found

    days_back_override: when set (by the scheduler's catch-up logic), overrides
    scan.days_back so a post-outage run covers the full gap.

    Returns a result dict suitable for the API response.
    """
    from ..models.item import Item
    from ..services.trademarks import search_recent_trademarks
    from ..services.enrichment import enrich_signal
    from ..extensions import db

    # Effective window — widened by the scheduler on catch-up runs
    days_back = days_back_override if days_back_override is not None else scan.days_back

    # ── 1. Fetch signals based on scan_type ───────────────────────────────────
    from ..services.delaware import search_recent_delaware_entities

    signals = []
    scan_type = getattr(scan, 'scan_type', 'full') or 'full'
    sources_ran = []
    errors = []

    def _collect(label: str, result: dict) -> None:
        """
        Merge one collector's output into this run.

        ALWAYS keeps the signals a collector managed to gather, even when it
        also reports an error. A mid-sweep upstream failure means FEWER
        filings, not bad ones — the old `if not error: extend(...)` pattern
        threw away every page already collected because page N returned a
        transient 500, which is how a single EDGAR blip cost a whole night of
        Form D coverage. The error is still recorded and lands on
        ScanRun.error_message, so a degraded source stays visible.
        """
        signals.extend(result.get("signals") or [])
        if result.get("error"):
            logger.warning("%s scan error: %s", label, result["error"])
            errors.append(f"{label}: {result['error']}")

    if scan_type in ('full', 'trademark'):
        sources_ran.append('trademark')
        tm_result = search_recent_trademarks(
            days_back=days_back,
            max_results=scan.max_results,
        )
        _collect("USPTO", tm_result)

    if scan_type in ('full', 'delaware'):
        sources_ran.append('delaware')
        de_result = search_recent_delaware_entities(
            days_back=days_back,
            max_results=max(500, scan.max_results),
        )
        _collect("Delaware", de_result)

    # Product Hunt — default-off in full scans (post-stealth corroborator; set
    # ENABLE_PRODUCTHUNT_SCAN=true to include in full scans).
    if scan_type == 'producthunt' or (scan_type == 'full' and os.environ.get('ENABLE_PRODUCTHUNT_SCAN')):
        sources_ran.append('producthunt')
        try:
            from ..services.producthunt import search_recent_producthunt
            ph_result = search_recent_producthunt(
                days_back=days_back,
                max_results=scan.max_results,
            )
            _collect("ProductHunt", ph_result)
        except Exception as exc:
            logger.warning("Product Hunt import/scan failed: %s", exc)
            errors.append(f"ProductHunt: {exc}")
    elif scan_type == 'full':
        logger.debug("Product Hunt skipped in full scan (ENABLE_PRODUCTHUNT_SCAN not set)")

    # App Store — default-off in full scans (post-stealth corroborator; set
    # ENABLE_APP_STORE_SCAN=true to include in full scans).
    if scan_type == 'app_store' or (scan_type == 'full' and os.environ.get('ENABLE_APP_STORE_SCAN')):
        sources_ran.append('app_store')
        try:
            from ..services.app_store import search_recent_app_store
            as_result = search_recent_app_store(
                days_back=days_back,
                max_results=scan.max_results,
            )
            _collect("AppStore", as_result)
        except Exception as exc:
            logger.warning("App Store import/scan failed: %s", exc)
            errors.append(f"AppStore: {exc}")
    elif scan_type == 'full':
        logger.debug("App Store skipped in full scan (ENABLE_APP_STORE_SCAN not set)")

    if scan_type in ('full', 'newswire'):
        sources_ran.append('newswire')
        try:
            from ..services.newswire import search_recent_newswire
            nw_result = search_recent_newswire(
                days_back=days_back,
                max_results=scan.max_results,
            )
            _collect("Newswire", nw_result)
        except Exception as exc:
            logger.warning("Newswire import/scan failed: %s", exc)
            errors.append(f"Newswire: {exc}")

    if scan_type in ('full', 'ctlogs'):
        sources_ran.append('ctlogs')
        try:
            from ..services.ctlogs import search_recent_ct_domains
            ct_result = search_recent_ct_domains(
                days_back=days_back,
                max_results=scan.max_results,
            )
            _collect("CTLogs", ct_result)
        except Exception as exc:
            logger.warning("CT logs import/scan failed: %s", exc)
            errors.append(f"CTLogs: {exc}")

    if scan_type in ('full', 'press_stealth'):
        sources_ran.append('press_stealth')
        try:
            from ..services.press_stealth import search_recent_press_stealth
            ps_result = search_recent_press_stealth(
                days_back=days_back,
                max_results=scan.max_results,
            )
            _collect("PressStealth", ps_result)
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
                "_type":            "signal",
                "fp":               fp,
                "company_name":     sig["companyName"],
                "signal_type":      signal_type,
                "category":         sig["category"],
                "score_boost":      sig.get("score_boost", 5),
                "is_intent_to_use": sig.get("is_intent_to_use"),  # trademark: 1b pre-launch flag
                "description":      sig["description"],
                "url":              sig["url"],
                "notes":            sig.get("notes", ""),
                "timestamp":        sig["timestamp"],
                # Form D enrichment — present only when _enrich_related_persons ran
                "related_persons":  sig.get("related_persons") or [],
                "filer_name":       sig.get("filer_name"),
                "conviction_match": sig.get("conviction_match"),
                "total_offering":   sig.get("total_offering"),
                "amount_sold":      sig.get("amount_sold"),
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
                "companyName":      meta.get("company_name", item.title),
                "category":         meta.get("category", ""),
                "signal_type":      meta.get("signal_type", "trademark"),
                "description":      meta.get("description", ""),
                "notes":            meta.get("notes", ""),
                "conviction_match": meta.get("conviction_match"),
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
                            filer_name=meta.get("filer_name") or None,
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
        from ..services.confluence import (
            record_signal_and_check_confluence,
            send_confluence_alert_for_hit,
            should_send_confluence_alert,
        )

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
                from ..services.confluence import extract_person_keys, extract_coined_term_keys
                _person_names = (
                    [rp["name"] for rp in meta.get("related_persons", []) if rp.get("name")]
                    + [n for n in [meta.get("owner"), meta.get("filer_name")] if n]
                )
                result = record_signal_and_check_confluence(
                    item_id=item_id,
                    owner_id=user_id,
                    brand_name=meta.get("company_name", item.title),
                    signal_type=meta.get("signal_type", "trademark"),
                    source_url=meta.get("url"),
                    enrichment=enrichment if enrichment.get("enriched") else None,
                    person_keys=extract_person_keys(_person_names),
                    coined_term_keys=extract_coined_term_keys(
                        meta.get("company_name") or item.title
                    ),
                )
                _conf_score = enrichment.get("bullish_score")
                if result["is_confluence"] and result.get("hit_id") and confluence_alert_emails:
                    if should_send_confluence_alert(_conf_score):
                        send_confluence_alert_for_hit(result["hit_id"], confluence_alert_emails)
                        logger.info("Confluence alert sent for %s (score=%s)", item.title, _conf_score)
                    else:
                        logger.info(
                            "Confluence alert suppressed for %s (score=%s) — below threshold or unscored",
                            item.title, _conf_score,
                        )
            except Exception as exc:
                logger.warning("Confluence check failed for item %s: %s", item_id, exc)
    except Exception as exc:
        logger.warning("Confluence detection block failed: %s", exc)

    # ── 5b. Immediate watchlist-match alerts ─────────────────────────────────
    # Fires the moment a Form D (or other signal with person names) names someone
    # from the 193-person conviction/alumni watchlist.  No press article needed —
    # this is the earliest possible signal, typically 6-24 months before any coverage.
    try:
        from ..services.email import send_watchlist_match_alert

        _wm_emails_str = os.environ.get("ALERT_EMAILS", "").strip()
        try:
            _wm_settings = Item.query.filter_by(title="__bullish_settings__").first()
            if _wm_settings:
                _wm_sd = json.loads(_wm_settings.description or "{}")
                _wm_el = _wm_sd.get("alert_emails", [])
                if _wm_el:
                    _wm_emails_str = ",".join(_wm_el)
        except Exception:
            pass
        _wm_alert_emails = [e.strip() for e in _wm_emails_str.split(",") if e.strip()]

        for item_id in new_item_ids:
            item = db.session.get(Item, item_id)
            if not item:
                continue
            try:
                meta       = json.loads(item.description or "{}")
                conviction = meta.get("conviction_match")
                alumni     = meta.get("exit_alumni_match")
                if not conviction and not alumni:
                    continue
                match      = conviction or alumni
                match_type = "CONVICTION" if conviction else "ALUMNI"
                enrichment = meta.get("enrichment") or {}
                for addr in _wm_alert_emails:
                    try:
                        send_watchlist_match_alert(
                            addr,
                            person_name=match.get("name", "Unknown"),
                            match_type=match_type,
                            brand_name=meta.get("company_name", item.title),
                            signal_type=meta.get("signal_type", "unknown"),
                            brand_score=enrichment.get("bullish_score"),
                            watch_level=enrichment.get("watch_level"),
                            thesis=enrichment.get("one_line_thesis", ""),
                            match_details=match,
                        )
                        logger.info(
                            "Watchlist match alert sent for %s (%s match: %s)",
                            item.title, match_type, match.get("name"),
                        )
                    except Exception as exc:
                        logger.warning("Watchlist match alert failed to %s: %s", addr, exc)
            except Exception as exc:
                logger.warning("Watchlist match alert check failed for item %s: %s", item_id, exc)
    except Exception as exc:
        logger.warning("Watchlist match alert block failed: %s", exc)

    # ── 5c. Auto-add HOT brands to watchlist ─────────────────────────────────
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

_MAX_CATCHUP_DAYS = 30  # hard ceiling so no single catch-up floods the enrichment budget


def _write_scheduler_heartbeat(app):
    """Persist a heartbeat after a successful scheduler run so status endpoints can report health."""
    from ..models.item import Item
    from ..extensions import db
    title = "__scheduler_heartbeat__"
    now = datetime.now(timezone.utc)
    with app.app_context():
        try:
            row = Item.query.filter_by(title=title, item_type="system").first()
            if row:
                row.description = json.dumps({"last_run": now.isoformat()})
            else:
                owner_id = _system_owner_id()
                if owner_id is None:
                    logger.error(
                        "Heartbeat write skipped: no user row to own the system item"
                    )
                    return
                db.session.add(Item(
                    title=title, item_type="system", owner_id=owner_id,
                    description=json.dumps({"last_run": now.isoformat()}),
                ))
            db.session.commit()
        except Exception as exc:
            logger.warning("Heartbeat write failed: %s", exc)


def get_scheduler_heartbeat(app) -> dict:
    """Return the last scheduler heartbeat and derived health status."""
    from ..models.item import Item
    now = datetime.now(timezone.utc)
    with app.app_context():
        try:
            row = Item.query.filter_by(title="__scheduler_heartbeat__", item_type="system").first()
            if not row:
                return {"last_run": None, "hours_since": None, "is_healthy": False}
            meta = json.loads(row.description or "{}")
            last_run_str = meta.get("last_run")
            if not last_run_str:
                return {"last_run": None, "hours_since": None, "is_healthy": False}
            last_run = datetime.fromisoformat(last_run_str)
            hours_since = (now - last_run).total_seconds() / 3600
            return {
                "last_run": last_run_str,
                "hours_since": round(hours_since, 1),
                "is_healthy": hours_since < 30,  # should run every 24h; 30h = one missed + buffer
            }
        except Exception as exc:
            logger.warning("Heartbeat read failed: %s", exc)
            return {"last_run": None, "hours_since": None, "is_healthy": False}


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

            # Catch-up: if we've been down longer than the normal window, widen days_back
            # so filings from the gap aren't permanently lost.
            days_back_override = None
            if scan.last_run_at:
                gap_days = (datetime.now(timezone.utc) - scan.last_run_at).days
                if gap_days > scan.days_back:
                    days_back_override = min(gap_days + 2, _MAX_CATCHUP_DAYS)
                    logger.info(
                        "Scan %s: %d-day gap detected — widening days_back %d → %d for catch-up",
                        scan.id, gap_days, scan.days_back, days_back_override,
                    )

            try:
                logger.info("Running scheduled scan %s for user %s", scan.id, scan.owner_id)
                run_scan_now(scan, scan.owner_id, days_back_override=days_back_override)
            except Exception as exc:
                logger.error("Scheduled scan %s failed: %s", scan.id, exc)

    _write_scheduler_heartbeat(app)


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

        # Filter out brands already sent in the previous digest to avoid re-sending
        # the same list when no new signals surfaced this week.
        try:
            from ..models.item import Item as _SettingsItem
            _digest_settings = _SettingsItem.query.filter_by(title="__bullish_settings__").first()
            _prev_brands: set = set()
            if _digest_settings:
                _ds = json.loads(_digest_settings.description or "{}")
                _prev_brands = set(_ds.get("digest_last_brands", []))
        except Exception:
            _prev_brands = set()

        def _is_new(sig):
            key = (sig.get("name") or "").upper().strip()
            prev_score = None
            for pb in (_prev_brands or set()):
                if isinstance(pb, str) and pb == key:
                    return False
                if isinstance(pb, list) and pb[0] == key:
                    prev_score = pb[1]
                    break
            return True

        hot_new  = [s for s in hot_signals  if _is_new(s)]
        warm_new = [s for s in warm_signals if _is_new(s)]

        if not hot_new and not warm_new:
            logger.info("Weekly digest: all HOT/WARM brands already sent last week — skipping")
            return

        hot_signals  = hot_new
        warm_signals = warm_new

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
        sent_ok = False
        for addr in [e.strip() for e in alert_emails.split(",") if e.strip()]:
            try:
                send_weekly_digest_email(addr, hot_signals[:5], warm_signals[:5], week_label)
                logger.info("Weekly digest sent to %s", addr)
                sent_ok = True
            except Exception as exc:
                logger.warning("Weekly digest email failed to %s: %s", addr, exc)

        # Persist the brands we just sent so next Monday's digest skips them if unchanged.
        if sent_ok:
            try:
                from ..models.item import Item as _SI2
                from ..extensions import db as _db2
                _sent_keys = [
                    (s.get("name") or "").upper().strip()
                    for s in (hot_signals[:5] + warm_signals[:5])
                ]
                _s2 = _SI2.query.filter_by(title="__bullish_settings__").first()
                if _s2:
                    _sd2 = json.loads(_s2.description or "{}")
                    _sd2["digest_last_brands"] = _sent_keys
                    _s2.description = json.dumps(_sd2, separators=(",", ":"))
                    _db2.session.commit()
            except Exception as exc:
                logger.warning("Digest: failed to save sent-brand cache: %s", exc)


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

        # Cap per-run: sort by stalest last_news_check first so coverage rotates across
        # the full watchlist over time, then limit to 50 per weekly run (~50 SerpAPI
        # credits/run vs 193 previously). Prevents blowing through 250/month budget in
        # a single Wednesday job.
        watchlist_items.sort(key=lambda x: x[1].get('last_news_check') or '')
        watchlist_items = watchlist_items[:50]
        logger.info("Founder news: processing %d watchlist people (capped at 50)", len(watchlist_items))

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

            # Skip muted entries (e.g. brands marked "too far along" or "too established").
            # Set muted:true on the watchlist item meta to suppress without deleting.
            if meta.get('muted'):
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

                # Normalised company name for confirmation matching (lowercase, no punctuation).
                company_slug = company.lower().replace('-', ' ').replace("'", "")

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
                    # Require a parseable date; if the date is missing or unparseable,
                    # skip — old articles frequently have unparseable date strings.
                    article_date = _parse_article_date(date_str)
                    if article_date is None or article_date < cutoff:
                        continue

                    # Company-name confirmation: require the brand/company to appear in
                    # title or snippet. This prevents name-collision false positives where
                    # a different person with the same name appears in unrelated articles
                    # (e.g. an obituary for a different "Vu Nguyen"). Only skip if the
                    # article has neither the company name nor specific new-venture
                    # language. "building" is intentionally excluded — it appears in
                    # too many unrelated contexts ("building a case", "building a community").
                    full_text = (title + " " + snippet).lower()
                    has_company = company_slug in full_text
                    has_new_venture = any(kw in full_text for kw in [
                        'new startup', 'new company', 'new brand', 'new venture',
                        'seed round', 'pre-seed', 'left to found', 'left to build',
                        'co-founded', 'co-founder', 'announced today', 'stealth',
                        'raises $', 'raised $', 'series a', 'series b',
                    ])
                    if not has_company and not has_new_venture:
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


def _run_watchlist_headline_sweep(app):
    """
    Monthly job (1st of month, 08:00 UTC): fetch current LinkedIn headlines for
    every watchlist person with a known linkedin_url and flag stealth-shift language.

    Stores each person's last seen headline in their watchlist item metadata.
    Fires an email alert when a headline changes to stealth-sounding language.
    Degrades silently if FreshData is unavailable or PROXYCURL_API_KEY is unset.
    """
    if not _acquire_job_lock(app, "watchlist_headline_sweep", ttl_seconds=60 * 60 * 24 * 25):
        logger.info("Watchlist headline sweep: already ran this month — skipping")
        return

    from ..models.item import Item
    from ..extensions import db

    with app.app_context():
        from .proxycurl import fetch_linkedin_headline, is_stealth_headline

        api_key = os.environ.get("PROXYCURL_API_KEY", "")
        if not api_key:
            logger.info("Watchlist headline sweep: PROXYCURL_API_KEY not set — skipping")
            return

        rows = Item.query.filter_by(item_type="watchlist").all()

        candidates = []
        for row in rows:
            try:
                meta = json.loads(row.description or "{}")
                url = meta.get("linkedin_url") or meta.get("linkedin") or ""
                if url and url.startswith("http"):
                    candidates.append((row, meta, url))
            except Exception:
                pass

        if not candidates:
            logger.info("Watchlist headline sweep: no watchlist people with linkedin_url — skipping")
            return

        logger.info("Watchlist headline sweep: checking %d people", len(candidates))

        stealth_hits = []
        checked = 0

        for row, meta, linkedin_url in candidates:
            result = fetch_linkedin_headline(linkedin_url)
            checked += 1
            if not result:
                continue

            headline      = result.get("headline", "")
            prev_headline = meta.get("_last_headline", "")

            meta["_last_headline"]       = headline
            meta["_headline_checked_at"] = datetime.now(timezone.utc).isoformat()
            row.description = json.dumps(meta, separators=(",", ":"))

            if is_stealth_headline(headline) and headline != prev_headline:
                person_name = meta.get("name") or row.title
                stealth_hits.append({
                    "name":         person_name,
                    "headline":     headline,
                    "prev_headline": prev_headline,
                    "linkedin_url": linkedin_url,
                    "designation":  meta.get("designation", ""),
                    "exit_brand":   meta.get("exit_brand", ""),
                })
                logger.warning(
                    "Watchlist headline sweep: STEALTH SHIFT — %s → %r",
                    person_name, headline,
                )

        db.session.commit()
        logger.info(
            "Watchlist headline sweep: checked %d people, %d stealth-shift hits",
            checked, len(stealth_hits),
        )

        if not stealth_hits:
            return

        # Email alert — reuse founder_news_alert format (each hit = one synthetic article)
        alert_emails = os.environ.get("ALERT_EMAILS", "").strip()
        try:
            settings = Item.query.filter_by(title="__bullish_settings__").first()
            if settings:
                s = json.loads(settings.description or "{}")
                if s.get("alert_emails"):
                    alert_emails = ",".join(s["alert_emails"])
        except Exception:
            pass

        if not alert_emails:
            return

        try:
            from .email import send_founder_news_alert
            for hit in stealth_hits:
                articles = [{
                    "title":   f"LinkedIn headline: {hit['headline']}",
                    "link":    hit["linkedin_url"],
                    "snippet": (
                        f"Was: {hit['prev_headline'] or 'unknown'} | "
                        f"Designation: {hit['designation']} | "
                        f"Exit brand: {hit['exit_brand'] or 'n/a'}"
                    ),
                    "date":   datetime.now(timezone.utc).strftime("%b %d, %Y"),
                    "source": "LinkedIn Watchlist Sweep",
                }]
                for addr in [e.strip() for e in alert_emails.split(",") if e.strip()]:
                    try:
                        send_founder_news_alert(
                            addr,
                            founder_name=hit["name"],
                            company=hit["exit_brand"] or "stealth",
                            bullish_score=None,
                            new_articles=articles,
                            linkedin_url=hit["linkedin_url"],
                        )
                    except Exception as exc:
                        logger.warning("Watchlist stealth alert email failed to %s: %s", addr, exc)
        except Exception as exc:
            logger.warning("Watchlist stealth alert: could not send email: %s", exc)


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


def _send_linkedin_poll_reminder(app):
    """Quarterly job (Jan/Apr/Jul/Oct 1 at 09:00 UTC): email admins to run the LinkedIn network poll."""
    logger.info("Running quarterly LinkedIn poll reminder")

    # ~85-day TTL prevents double-firing if the scheduler restarts near a quarter boundary
    if not _acquire_job_lock(app, "quarterly_linkedin_poll_reminder", ttl_seconds=60 * 60 * 24 * 85):
        return

    with app.app_context():
        from ..models.user import User
        from ..models.item import Item
        from ..services.email import send_linkedin_poll_reminder_email
        from datetime import timezone, timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=365 * 3)
        rows   = Item.query.filter(Item.item_type == 'watchlist').all()
        eligible = 0
        for row in rows:
            try:
                meta = json.loads(row.description or '{}')
                if meta.get('_type') != 'watchlist':
                    continue
                if meta.get('source') != 'linkedin_import':
                    continue
                if not meta.get('linkedin'):
                    continue
                cd = meta.get('connected_date', '')
                if cd:
                    try:
                        from datetime import timezone as _tz
                        if datetime.strptime(cd, '%d %b %Y').replace(tzinfo=_tz.utc) < cutoff:
                            continue
                    except ValueError:
                        pass
                eligible += 1
            except Exception:
                continue

        estimated_cost = round(eligible * 0.0025, 2)  # FreshData: ~$0.0025/profile
        settings_url   = os.environ.get("FRONTEND_URL", "https://brentvartan.github.io/stealth-finder-frontend") + "/#/settings"

        for u in User.query.filter_by(role='admin').all():
            try:
                send_linkedin_poll_reminder_email(
                    to_email=u.email,
                    eligible_count=eligible,
                    estimated_cost=estimated_cost,
                    settings_url=settings_url,
                )
                logger.info("LinkedIn poll reminder sent to %s", u.email)
            except Exception as exc:
                logger.warning("Failed to send LinkedIn poll reminder to %s: %s", u.email, exc)


def _send_founder_radar_poll_reminder(app):
    """Quarterly job (Jan/Apr/Jul/Oct 1 at 09:30 UTC): email admins to run the Founder Radar poll."""
    logger.info("Running quarterly Founder Radar poll reminder")

    if not _acquire_job_lock(app, "quarterly_founder_radar_poll_reminder", ttl_seconds=60 * 60 * 24 * 85):
        return

    with app.app_context():
        from ..models.user import User
        from ..services.email import send_founder_radar_poll_reminder_email
        from ..services.founder_radar import get_poll_people, count_by_tier

        people     = get_poll_people()
        n          = len(people)
        by_tier    = count_by_tier()
        est_cost   = round(n * 3 * 0.01, 2)
        settings_url = (
            os.environ.get("FRONTEND_URL", "https://brentvartan.github.io/stealth-finder-frontend")
            + "/#/settings"
        )

        for u in User.query.filter_by(role="admin").all():
            try:
                send_founder_radar_poll_reminder_email(
                    to_email=u.email,
                    people_count=n,
                    by_tier=by_tier,
                    estimated_cost=est_cost,
                    settings_url=settings_url,
                )
                logger.info("Founder Radar poll reminder sent to %s", u.email)
            except Exception as exc:
                logger.warning("Failed to send Founder Radar poll reminder to %s: %s", u.email, exc)


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
        # 11:00 UTC = 07:00 ET. The old 06:00 UTC (02:00 ET) slot landed inside
        # SEC EDGAR's overnight maintenance window, where efts.sec.gov returns
        # intermittent 500s — that alone killed 8 of the last 10 Form D passes.
        # 07:00 ET is comfortably in EDGAR's healthy window and still lands
        # before the US workday.
        _scheduler.add_job(
            _run_all_scheduled,
            trigger=CronTrigger(hour=11, minute=0, timezone="UTC"),
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
        # _check_founder_news (Wed 08:00 UTC) removed 2026-07-22:
        # Google News search on 193 people consumed 77% of 250/month SerpAPI budget
        # in a single run, and alerted on already-public press (too late to act).
        # Replaced by immediate watchlist-match alerts in run_scan_now (step 5b)
        # which fire the moment a Form D names a conviction/alumni person.
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
        _scheduler.add_job(
            _run_watchlist_headline_sweep,
            trigger=CronTrigger(day=1, hour=8, minute=0, timezone="UTC"),
            args=[app],
            id="monthly_watchlist_headline_sweep",
            replace_existing=True,
            misfire_grace_time=86400,
        )
        _scheduler.add_job(
            _send_linkedin_poll_reminder,
            trigger=CronTrigger(month="1,4,7,10", day=1, hour=9, minute=0, timezone="UTC"),
            args=[app],
            id="quarterly_linkedin_poll_reminder",
            replace_existing=True,
            misfire_grace_time=86400,
        )
        _scheduler.add_job(
            _send_founder_radar_poll_reminder,
            trigger=CronTrigger(month="1,4,7,10", day=1, hour=9, minute=30, timezone="UTC"),
            args=[app],
            id="quarterly_founder_radar_poll_reminder",
            replace_existing=True,
            misfire_grace_time=86400,
        )
        _scheduler.start()
        logger.info(
            "Bullish scheduler started — daily scan 11:00 UTC, weekly digest Mon 09:00 UTC, "
            "founder news Wed 08:00 UTC, press monitor Thu 08:00 UTC, "
            "inbox audit reminder 1st 07:00 UTC, watchlist headline sweep 1st 08:00 UTC, "
            "quarterly LinkedIn poll reminder Jan/Apr/Jul/Oct 1 09:00 UTC, "
            "quarterly Founder Radar poll reminder Jan/Apr/Jul/Oct 1 09:30 UTC"
        )
    except Exception as exc:
        logger.warning("Scheduler could not start: %s", exc)
