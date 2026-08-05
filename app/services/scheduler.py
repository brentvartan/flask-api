"""
Bullish Stealth Finder — Background Scheduler Service.

Runs daily USPTO scans, enriches new signals with Bullish AI,
and fires HOT-signal email alerts to the team.
"""
import os
import json
import re
import hashlib
import logging
import threading

from datetime import datetime, timezone, timedelta
from flask import current_app

logger = logging.getLogger(__name__)

# Module-level flag so only ONE scheduler starts per process
_scheduler = None


# Fingerprints are written by json.dumps with compact separators, but tolerate
# whitespace in case a row was ever written by a different writer.
_FP_RE = re.compile(r'"fp"\s*:\s*"([0-9a-fA-F]{8,64})"')


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


# ─── USPTO trademark sweep budget + watermark ─────────────────────────────────

# Per-run returned-signal budget for the trademark collector.
#
# ScheduledScan.max_results defaults to 200, which is roughly a fifth of ONE
# day's genuine consumer candidates (~1,050/day measured live 2026-07-26). With
# the sweep sorted filedDate DESC, a budget that small truncates every run
# mid-day, and the remainder is a permanent miss because the next run's sweep
# starts from a newer horizon. The floor is set above a peak day with margin;
# cheap triage downstream is what makes a budget this size affordable.
# max() so an operator who deliberately configured a LARGER scan still wins.
_TRADEMARK_SIGNAL_BUDGET = 9000

# Control row holding the trademark sweep watermark (see trademarks.py — USPTO
# indexes filings days-to-weeks after their filedDate, so a filedDate-windowed
# sweep alone loses the late tail forever).
_TM_WATERMARK_TITLE = "__tm_load_watermark__"


def _read_trademark_watermark():
    """
    Return the datetime of the last clean trademark sweep, or None.

    None means "no watermark yet" and the collector simply skips its
    late-arrival pass — a first run has nothing to catch up on.
    Must be called inside an app context.
    """
    from ..models.item import Item
    try:
        row = Item.query.filter_by(title=_TM_WATERMARK_TITLE, item_type="system").first()
        if not row:
            return None
        raw = json.loads(row.description or "{}").get("swept_at")
        return datetime.fromisoformat(raw) if raw else None
    except Exception as exc:
        logger.warning("Trademark watermark read failed: %s — sweeping without it", exc)
        return None


def _write_trademark_watermark(swept_at: str) -> None:
    """
    Persist the watermark returned by a CLEAN trademark sweep.

    trademarks.py returns swept_at=None when the sweep errored part-way, and the
    caller must not advance the watermark in that case: the failed run never saw
    everything loaded before its start time, and moving the watermark past those
    records would silently recreate the permanent miss.
    Must be called inside an app context.
    """
    from ..models.item import Item
    from ..extensions import db
    if not swept_at:
        return
    try:
        row = Item.query.filter_by(title=_TM_WATERMARK_TITLE, item_type="system").first()
        if row:
            row.description = json.dumps({"swept_at": swept_at})
        else:
            owner_id = _system_owner_id()
            if owner_id is None:
                logger.error("Trademark watermark write skipped: no user row to own it")
                return
            db.session.add(Item(
                title=_TM_WATERMARK_TITLE, item_type="system", owner_id=owner_id,
                description=json.dumps({"swept_at": swept_at}),
            ))
        db.session.commit()
    except Exception as exc:
        logger.warning("Trademark watermark write failed: %s", exc)


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


# ─── Scan progress telemetry ──────────────────────────────────────────────────

_SCAN_PROGRESS_TITLE = "__scan_progress__"


def _scan_progress(phase: str, **detail) -> None:
    """
    Record which phase a running scan is in, on its OWN connection.

    Deliberately does not touch db.session. Sharing the scan's session made the
    telemetry both a liar and a hazard: the ORM query autoflushes whatever
    inserts are pending, and the except-and-rollback here would then discard
    them — so a progress write could silently undo a chunk of the scan it was
    meant to be reporting on. A short-lived connection with a plain UPSERT can
    do neither.

    Best-effort and never raises: telemetry must not be able to break a scan.
    """
    from ..extensions import db
    from sqlalchemy import text

    payload = json.dumps({"phase": phase,
                          "at": datetime.now(timezone.utc).isoformat(), **detail})
    try:
        with db.engine.begin() as conn:
            updated = conn.execute(
                text("UPDATE items SET description = :d, updated_at = now() "
                     "WHERE title = :t AND item_type = 'system'"),
                {"d": payload, "t": _SCAN_PROGRESS_TITLE},
            ).rowcount
            if not updated:
                owner = conn.execute(
                    text("SELECT id FROM users ORDER BY (role <> 'admin'), id LIMIT 1")
                ).scalar()
                if owner is None:
                    return
                conn.execute(
                    text("INSERT INTO items (title, description, item_type, owner_id, "
                         "created_at, updated_at) VALUES (:t, :d, 'system', :o, now(), now())"),
                    {"t": _SCAN_PROGRESS_TITLE, "d": payload, "o": owner},
                )
    except Exception as exc:
        logger.debug("Scan progress write failed (%s): %s", phase, exc)


def read_scan_progress() -> dict:
    """Return the last recorded scan phase (empty dict when never run)."""
    from ..models.item import Item
    try:
        row = Item.query.filter_by(title=_SCAN_PROGRESS_TITLE, item_type="system").first()
        return json.loads(row.description or "{}") if row else {}
    except Exception:
        return {}


# ─── Save / enrich budgets ────────────────────────────────────────────────────

# Rows per commit in the save loop. Deep paging returns ~8,500 signals a run;
# one transaction around all of them left every row invisible and losable until
# the end of a long loop.
_SAVE_CHUNK = 500

# How many signals one run may SCORE. Collection is deliberately unbounded and
# enrichment deliberately is not: saving is cheap and is what guarantees
# coverage, while scoring costs an API call per signal. A saved-but-unscored
# signal is never lost — it sits in the corpus and a later run picks it up — so
# capping this trades latency for predictable cost and runtime, not coverage.
#
# RAISED 2026-08-03 (Brent) from 2,000 to 3,000, for one month, to DRAIN a
# backlog that could not otherwise clear. The arithmetic: a run was saving
# ~2,230 new signals a night against a 2,000 budget, so new arrivals consumed
# the entire budget every day and the 5,814 never-assessed signals — 29% of the
# corpus — were unreachable and growing. An unassessed signal is invisible to
# conviction matching, confluence and every alert, not merely unscored, so that
# was 29% of the corpus unable to produce a result at all.
#
# At 3,000 the surplus is ~770/day, clearing the backlog in roughly 8 days.
# Review at _BUDGET_REVIEW_ON — the Monday digest raises it (see
# scheduler._budget_review_due) so the decision surfaces itself rather than
# depending on anyone remembering.
_DEFAULT_ENRICH_BUDGET = 3000

# Concurrent scoring workers. Each signal is dominated by blocking Anthropic
# calls, so this is IO-bound. Kept small on purpose: the SQLAlchemy pool is
# 5 + 10 overflow per process and every worker holds a connection.
_SCORE_WORKERS = max(1, int(os.environ.get("SCAN_SCORE_WORKERS", "4")))


def _enrich_budget() -> int:
    """Per-run scoring budget, overridable with SCAN_ENRICH_BUDGET."""
    raw = os.environ.get("SCAN_ENRICH_BUDGET", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            logger.warning("SCAN_ENRICH_BUDGET=%r is not an integer — using default", raw)
    return _DEFAULT_ENRICH_BUDGET


# ─── Elevated-budget review ───────────────────────────────────────────────────

from datetime import date as _date

_BUDGET_RAISED_ON  = _date(2026, 8, 3)     # 2,000 -> 3,000
_BUDGET_TRIAL_FROM = 2000
_BUDGET_TRIAL_TO   = 3000

# THE RUN CAP. Brent capped this on 2026-08-04: run two more weeks, then decide
# rather than drift. The elevated-budget trial was originally a month
# (2026-09-03); it now ends with the cap, because two separate review dates is
# two chances to ignore both.
#
# What happens on this date is a DECISION, not a shutdown — scans keep running.
# The digest stops being a list of brands and leads with the question instead:
# is this earning its keep, and is anyone acting on it? Left unanswered the
# banner simply keeps appearing, which is the intended failure mode. A system
# quietly spending money that nobody reads should be loud about it.
_RUN_CAP_SET_ON = _date(2026, 8, 4)
_RUN_CAP_ON     = _date(2026, 8, 18)


def _run_cap_reached() -> bool:
    """True once the two-week cap is up. Idempotent and safe to leave in."""
    return _date.today() >= _RUN_CAP_ON


def _budget_review_due() -> bool:
    """
    True once the elevated-budget trial is up, which is now the run cap.

    Surfaced in the Monday digest rather than left as a note somewhere, because
    a reminder nobody reads is not a reminder. Goes quiet on its own if the
    budget is dropped back to 2,000 — no cleanup needed.
    """
    return _run_cap_reached() and _enrich_budget() > _BUDGET_TRIAL_FROM


def _backlog_item_ids(limit: int, exclude: set) -> list:
    """
    Oldest signals that were saved but never scored, for draining leftover budget.

    Un-scored rows carry no "enrichment" key at all, so a substring test on the
    JSON description finds them without a schema change. Oldest first: a signal
    that has waited longest is the one most at risk of being forgotten.
    """
    from ..models.item import Item

    if limit <= 0:
        return []
    try:
        rows = (
            Item.query
            .filter(Item.item_type == "signal")
            # "Never ASSESSED", not "never enriched". A signal the triage gate
            # rejected carries a "triage" key and NEVER acquires an "enrichment"
            # one — so filtering on enrichment alone re-selected every rejected
            # signal on every run, oldest-first, which is the front of the
            # queue. The backlog could never drain past them and each night paid
            # to re-triage the same rows forever.
            .filter(~Item.description.contains('"enrichment"'))
            .filter(~Item.description.contains('"triage"'))
            .order_by(Item.created_at.asc())
            .limit(limit + len(exclude))
            .with_entities(Item.id)
            .all()
        )
    except Exception as exc:
        logger.warning("Backlog lookup failed: %s — scoring new signals only", exc)
        return []
    return [r.id for r in rows if r.id not in exclude][:limit]


def _fair_share(item_ids: list, types_by_id: dict, budget: int) -> list:
    """
    Spread the scoring budget ACROSS collectors instead of down one list.

    new_item_ids is in collection order and the trademark sweep runs first, so a
    plain [:budget] slice handed the whole budget to trademarks: on a run saving
    ~3,000 trademarks against a 2,000 budget, every Form D signal collected that
    night got scored zero times. Form D is Job 1 — the one source that is
    supposed to be airtight — and an unscored signal is invisible to conviction
    matching, confluence, the watchlist and every alert, not just to the score.

    Round-robins by signal type, preserving each type's own newest-first order,
    so a low-volume source can never be starved by a high-volume one. Any budget
    a small source does not use falls through to the larger ones.
    """
    if budget <= 0 or not item_ids:
        return []
    buckets: dict = {}
    for iid in item_ids:
        buckets.setdefault(types_by_id.get(iid, "unknown"), []).append(iid)

    out, order = [], list(buckets)
    while len(out) < budget and any(buckets[t] for t in order):
        for t in order:
            if not buckets[t]:
                continue
            out.append(buckets[t].pop(0))
            if len(out) >= budget:
                break
    return out


# ─── Core: run a single scan now ──────────────────────────────────────────────

def run_scan_now(scan, user_id: int, days_back_override: int = None) -> dict:
    """
    Execute a ScheduledScan immediately:
      1. Fetch signals from every collector this scan_type covers
      2. Save new signals (deduplicated)
      3. Run signal_pipeline.process_saved_signal on each — the SAME shared
         pipeline the manual scan path uses (people matching, assignment
         resolution, Form D related persons, confluence, enrichment, alerts,
         watchlist). Nothing post-save belongs inline in this function.
      4. Roll the results up into the scan record and a ScanRun row

    days_back_override: when set (by the scheduler's catch-up logic), overrides
    scan.days_back so a post-outage run covers the full gap.

    Returns a result dict suitable for the API response.
    """
    from ..models.item import Item
    from ..services.trademarks import search_recent_trademarks
    from ..services.signal_pipeline import _safe_title
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

    _scan_progress("collect:trademark")
    if scan_type in ('full', 'trademark'):
        sources_ran.append('trademark')
        tm_result = search_recent_trademarks(
            days_back=days_back,
            max_results=max(_TRADEMARK_SIGNAL_BUDGET, scan.max_results),
            loaded_since=_read_trademark_watermark(),
        )
        _collect("USPTO", tm_result)
        # Advance only on a clean sweep — search_recent_trademarks returns
        # swept_at=None when it failed part-way, and _write_... ignores None.
        _write_trademark_watermark(tm_result.get("swept_at"))
        if tm_result.get("late_arrivals"):
            logger.info(
                "USPTO: %d late-indexed filings recovered by the watermark pass",
                tm_result["late_arrivals"],
            )

    _scan_progress("collect:delaware")
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

    _scan_progress("dedup:load-fingerprints", collected=len(signals))

    # ── 2. Load existing fingerprints (dedup) ─────────────────────────────────
    # STREAM the descriptions and pull the fingerprint out with a regex rather
    # than materialising every row and json.loads()-ing it.
    #
    # Measured on production: 8,853 signal rows averaging 4.3 KB each — ~36 MB of
    # raw text, and parsing each one into a nested dict (they carry the full
    # enrichment blob) costs several hundred MB of Python objects, transiently,
    # inside a container also running four gunicorn workers. The save loop that
    # takes 0.34s locally was taking over four minutes in production, examining
    # under 1,000 signals a minute, because the box was thrashing rather than
    # working. We need 16 characters per row; we were paying for all 4.3 KB.
    existing_fps = set()
    _fp_rows = (
        Item.query
        .filter(Item.item_type == "signal")
        .filter(Item.description.contains('"fp"'))
        .with_entities(Item.description)
        .yield_per(500)
    )
    for (desc,) in _fp_rows:
        m = _FP_RE.search(desc or "")
        if m:
            existing_fps.add(m.group(1))

    _scan_progress("save", collected=len(signals), known_fingerprints=len(existing_fps))

    # ── 3. Persist new signals ────────────────────────────────────────────────
    new_saved = 0
    new_item_ids = []
    item_signal_types: dict = {}     # item id -> signal_type, for fair budget sharing
    pending = []
    pending_types = []
    domain_pending = []

    def _flush_batch(batch: list) -> list:
        """Insert a batch in one round-trip and return the new ids."""
        if not batch:
            return []
        db.session.add_all(batch)
        db.session.flush()      # assigns every id in the batch
        ids = [i.id for i in batch]
        for iid, stype in zip(ids, pending_types):
            item_signal_types[iid] = stype
        pending_types.clear()
        db.session.commit()
        batch.clear()
        return ids

    for _seen, sig in enumerate(signals, 1):
        if _seen % 200 == 0:
            _scan_progress("save", collected=len(signals), examined=_seen, saved=new_saved)
        signal_type = sig.get("signal_type", "trademark")
        import re as _re
        _norm = _re.sub(r'\s+', ' ', sig['companyName'].upper().strip())
        key = f"{signal_type}:{_norm}:{sig['timestamp'][:10]}"
        fp  = hashlib.sha256(key.encode()).hexdigest()[:16]

        if fp in existing_fps:
            continue

        item = Item(
            title=_safe_title(sig["companyName"]),
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
                "brand_uncertain":  sig.get("brand_uncertain", False),
                "timestamp":        sig["timestamp"],
                # Trademark/press owner — feeds person-key confluence, the conviction
                # and alumni matchers, and the enrichment prompt's FOUNDER RESEARCH
                # PRIORITY block. Dropping it here is what made a conviction founder
                # named in a USPTO owner field invisible to the nightly scan.
                "owner":            sig.get("owner"),
                "owner_is_person":  sig.get("owner_is_person", False),
                # Form D enrichment — present only when _enrich_related_persons ran
                "related_persons":  sig.get("related_persons") or [],
                "filer_name":       sig.get("filer_name"),
                "conviction_match": sig.get("conviction_match"),
                # Alumni is a separate designation from conviction and must persist
                # separately, or the ALUMNI branch of the watchlist alert is unreachable.
                "exit_alumni_match": sig.get("exit_alumni_match"),
                "total_offering":   sig.get("total_offering"),
                "amount_sold":      sig.get("amount_sold"),
                # Parsed on every Form D fetch by parse_form_d_offering_data;
                # persisting it costs nothing and it is a real ranking feature
                # (a $25k minimum reads very differently from a $1M one).
                "minimum_investment": sig.get("minimum_investment"),
                # EDGAR filing IDs — without these the Form D related-persons
                # extraction can never run on the scheduled path.
                "_adsh":            sig.get("_adsh", ""),
                "_cik":             sig.get("_cik", ""),
            }, separators=(",", ":")),
        )
        pending.append(item)
        pending_types.append(signal_type)
        if signal_type == "domain" and sig.get("url"):
            domain_pending.append((item, sig["url"]))
        existing_fps.add(fp)
        new_saved += 1

        # Flush and commit in CHUNKS, not per item. The sweep now returns ~8,500
        # signals a run against the ~200 this path was written for, and the old
        # add()+flush() per signal meant one network round-trip to Postgres per
        # row: a measured production run spent over six minutes without saving
        # even 500 of ~3,000 new rows. One flush per chunk lets SQLAlchemy batch
        # the INSERTs and assigns every id in the batch, cutting thousands of
        # round-trips to a handful.
        #
        # Committing per chunk also matters on its own: one transaction around
        # the whole loop left every row invisible, and losable on a restart,
        # until the very end. Saving is the cheap half of the job and the half
        # that guarantees coverage, so it lands durably as it goes.
        if len(pending) >= _SAVE_CHUNK:
            new_item_ids.extend(_flush_batch(pending))
            _scan_progress("save", collected=len(signals), saved=new_saved)

    new_item_ids.extend(_flush_batch(pending))   # tail of the last chunk
    _scan_progress("save:done", collected=len(signals), saved=new_saved)
    if new_saved > 0:
        logger.info("Saved %d new signals (%d collected)", new_saved, len(signals))

    # ── 3b. Background domain checks for newly saved domain signals ───────────
    # Uses the handful of domain signals recognised during the save loop. It used
    # to re-read EVERY newly saved item back out of the database one at a time,
    # just to look at its signal_type — and because the session is expired after
    # each commit, every one of those was a real query. At ~200 signals a run
    # that was invisible; at ~3,000 new rows it was thousands of round-trips and
    # became the single slowest phase of the scan, all to find domain signals
    # that no collector currently emits.
    for _dom_item, _dom_url in domain_pending:
        try:
            threading.Thread(
                target=_check_domain_bg,
                args=(current_app._get_current_object(), _dom_item.id, _dom_url),
                daemon=True,
            ).start()
        except Exception as exc:
            logger.debug("Domain check launch failed: %s", exc)

    # ── 4. Shared post-save pipeline ─────────────────────────────────────────
    # Enrichment, confluence, watchlist-match alerts and HOT handling all live in
    # signal_pipeline.process_saved_signal, which the MANUAL scan path
    # (app/api/scans/routes.py) calls too. Both paths therefore do exactly the
    # same work on the same signal. Anything new belongs in that pipeline — the
    # old inline copies here (steps 4, 5, 5b and 5c) had silently diverged from
    # the manual path three times, always in the direction of the nightly scan
    # doing LESS.
    from ..services.signal_pipeline import process_saved_signal, resolve_alert_emails

    hot_brands = []
    hot_count  = 0
    warm_count = 0
    cold_count = 0
    founders_queued = 0

    # Resolved once per run — the pipeline would otherwise re-query settings per signal.
    pipeline_alert_emails = resolve_alert_emails()

    # Enrichment is BUDGETED; collection is not. Deep paging now saves ~8,500
    # signals a run, and scoring every one serially would take many hours and
    # cost accordingly. Coverage of the CORPUS does not depend on that: every
    # signal is already saved above, so nothing is ever lost by scoring it
    # later. Newest first (a fresh filing is the point of the product), then any
    # remaining budget drains the oldest un-scored rows so a backlog cannot sit
    # there forever. Tune with SCAN_ENRICH_BUDGET.
    _scan_progress("budget", saved=new_saved)
    enrich_budget = _enrich_budget()
    to_process = _fair_share(new_item_ids, item_signal_types, enrich_budget)
    if len(to_process) < enrich_budget:
        to_process += _backlog_item_ids(enrich_budget - len(to_process),
                                        exclude=set(new_item_ids))
    deferred = max(0, len(new_item_ids) - len(to_process))
    if deferred:
        logger.info(
            "Enrichment budget %d — scoring %d now, %d saved signals deferred to a later run",
            enrich_budget, len(to_process), deferred,
        )

    _scan_progress("score", saved=new_saved, to_score=len(to_process), deferred=deferred)

    # Score CONCURRENTLY. Each signal is dominated by two blocking network calls
    # (the Haiku gate, then Sonnet for survivors), so a serial loop ran at well
    # under 50 signals in 11 minutes measured in production — hours for a single
    # run's budget. The work is IO-bound and independent per signal, so a small
    # pool collapses that without touching the logic.
    #
    # Bounded deliberately: the SQLAlchemy pool is 5 + 10 overflow per process,
    # and each worker needs its own app context (Flask-SQLAlchemy 3.x scopes the
    # session to the app context, so threads must not share one). Results are
    # aggregated in THIS thread as futures land, so the counters and hot_brands
    # need no locking.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    _app_obj = current_app._get_current_object()

    def _score_one(iid):
        with _app_obj.app_context():
            try:
                return process_saved_signal(
                    iid, owner_id=user_id, alert_emails=pipeline_alert_emails,
                )
            except Exception as exc:
                logger.warning("Signal pipeline failed for item %s: %s", iid, exc)
                return None
            finally:
                db.session.remove()

    _scored = 0
    with ThreadPoolExecutor(max_workers=_SCORE_WORKERS) as _pool:
        _futures = [_pool.submit(_score_one, iid) for iid in to_process]
        for _fut in as_completed(_futures):
            outcome = _fut.result()
            if outcome is None:
                continue

            _scored += 1
            if _scored % 50 == 0:
                _scan_progress("score", saved=new_saved, scored=_scored,
                               to_score=len(to_process), deferred=deferred)

            if outcome.get("errors"):
                logger.warning("Signal pipeline degraded: %s", "; ".join(outcome["errors"]))
            if outcome.get("founder_queued"):
                founders_queued += 1
            if not outcome.get("enriched"):
                continue

            level = outcome.get("watch_level")
            if level == "hot":
                hot_count += 1
                hot_brands.append({
                    "name":     outcome.get("company_name"),
                    "category": outcome.get("category", ""),
                    "score":    outcome.get("bullish_score"),
                    "thesis":   outcome.get("one_line_thesis", ""),
                    "theme":    outcome.get("cultural_theme", ""),
                    "item_id":  outcome.get("item_id"),
                })
            elif level == "warm":
                warm_count += 1
            else:
                cold_count += 1

    if new_item_ids:
        db.session.commit()

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


def _read_scheduler_liveness() -> dict:
    """
    Return the scheduler-liveness marker written at startup.

    Distinguishes "no scheduler is running anywhere" from "a scheduler is armed
    but has not completed a run yet" — the heartbeat alone cannot tell those
    apart, which is how a dead scheduler went unnoticed for a day.
    Must be called inside an app context.
    """
    from ..models.item import Item
    try:
        row = Item.query.filter_by(title="__scheduler_liveness__", item_type="system").first()
        if not row:
            return {"scheduler_armed": False, "next_run_at": None, "jobs": {}}
        meta = json.loads(row.description or "{}")
        jobs = meta.get("jobs") or {}
        return {
            "scheduler_armed": True,
            "scheduler_started_at": meta.get("started_at"),
            "next_run_at": jobs.get("daily_bullish_scan"),
            "jobs": jobs,
        }
    except Exception as exc:
        logger.warning("Scheduler liveness read failed: %s", exc)
        return {"scheduler_armed": None, "next_run_at": None, "jobs": {}}


def get_scheduler_heartbeat(app) -> dict:
    """Return the last scheduler heartbeat and derived health status."""
    from ..models.item import Item
    now = datetime.now(timezone.utc)
    with app.app_context():
        try:
            row = Item.query.filter_by(title="__scheduler_heartbeat__", item_type="system").first()
            if not row:
                return {"last_run": None, "hours_since": None, "is_healthy": False,
                        **_read_scheduler_liveness(), "scan_progress": read_scan_progress()}
            meta = json.loads(row.description or "{}")
            last_run_str = meta.get("last_run")
            if not last_run_str:
                return {"last_run": None, "hours_since": None, "is_healthy": False,
                        **_read_scheduler_liveness(), "scan_progress": read_scan_progress()}
            last_run = datetime.fromisoformat(last_run_str)
            hours_since = (now - last_run).total_seconds() / 3600
            return {
                "last_run": last_run_str,
                "hours_since": round(hours_since, 1),
                "is_healthy": hours_since < 30,  # should run every 24h; 30h = one missed + buffer
                **_read_scheduler_liveness(),
                "scan_progress": read_scan_progress(),
            }
        except Exception as exc:
            logger.warning("Heartbeat read failed: %s", exc)
            return {"last_run": None, "hours_since": None, "is_healthy": False,
                        **_read_scheduler_liveness(), "scan_progress": read_scan_progress()}


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
        ran_any = False
        for scan in scans:
            # Skip if already run within the cooldown window
            if scan.last_run_at:
                # The cooldown exists to stop a double-fire (two workers, a
                # restart, a manual trigger minutes before the cron), NOT to
                # ration daily runs. It is measured from when the last run
                # ENDED, so at 20h against a 24h cron any run whose wall-clock
                # exceeds 4 hours pushes the next night inside the window and
                # that night is skipped entirely. Runs now routinely exceed 4h
                # (a 2,000-signal scoring budget), so a "daily" scan was
                # quietly becoming every-other-day. Half the cron interval is
                # the honest bound: it still absorbs a double-fire and cannot
                # eat a scheduled night.
                cooldown_hours = 11 if scan.frequency == "daily" else 84
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
                ran_any = True
            except Exception as exc:
                logger.error("Scheduled scan %s failed: %s", scan.id, exc)
                try:
                    _scan_progress("failed", error=f"{type(exc).__name__}: {exc}")
                except Exception:
                    pass


    # Only claim a run when one actually happened. This used to fire even when
    # every scan was skipped by the cooldown `continue` above, so the status
    # endpoint reported a fresh, healthy run for a job that did no work — the
    # exact class of lying instrument this whole audit was about.
    if ran_any:
        _write_scheduler_heartbeat(app)
    else:
        logger.info("Scheduler tick: every scan skipped — heartbeat NOT advanced")


def _send_weekly_digest(app):
    """APScheduler job — every Monday 9:00 UTC. Sends top HOT/WARM signals from the past 7 days."""
    if not _acquire_job_lock(app, "weekly_digest", ttl_seconds=82800):  # 23h — one per day max
        logger.info("Weekly digest: already sent by another worker — skipping")
        return
    from ..models.item import Item
    from ..services.email import send_weekly_digest_email
    from ..extensions import db
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
                # The digest presents a NAME as a brand. A press signal whose
                # name is really the article headline is a genuine signal but
                # not a brand, and listing it beside real names and scores reads
                # as noise — two of the ten entries in the 2026-07-27 digest
                # were headlines ("SNACK BARS WERE NEVER REFRESHING. UNTIL
                # NOW."). It stays fully visible on the dashboard; it just does
                # not get presented here as something it is not.
                if meta.get("brand_uncertain"):
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

        # NOTE: no early return here even when hot/warm are empty. The digest is
        # now the ONLY channel for deferred confluence hits and watchlist people
        # matches (weekly alert delivery, 2026-07-30) — those are collected
        # below, and the combined skip check after them decides whether there is
        # genuinely nothing to send.

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

        # Order by learned theme affinity, not by score. Brent's 2026-08-03
        # triage showed score predicts MEMBERSHIP well and ORDER not at all —
        # his top-3 picks overlapped the model's top 3 on 7 of 24, where random
        # is 30%. Theme did predict them. rank_signals never drops an entry; it
        # only reorders, so the digest still shows what it always would.
        from ..services.ranking import rank_signals
        try:
            hot_signals  = rank_signals(hot_signals)
            warm_signals = rank_signals(warm_signals)
        except Exception as exc:
            logger.warning("Digest ranking failed (%s) — falling back to score order", exc)
            hot_signals  = sorted(hot_signals,  key=lambda e: -(e.get("score") or 0))
            warm_signals = sorted(warm_signals, key=lambda e: -(e.get("score") or 0))

        hot_new  = [s for s in hot_signals  if _is_new(s)]
        warm_new = [s for s in warm_signals if _is_new(s)]

        # ── Deferred per-event alerts (weekly delivery mode) ─────────────────
        # Confluence hits queue as alert_sent=False rows; conviction/alumni
        # matches live on the signals themselves. The digest is now the ONE
        # place these reach the inbox (Brent, 2026-07-30), so it must carry
        # everything the per-event emails used to.
        from ..models.confluence_hit import ConfluenceHit

        confluence_hits = []
        try:
            _pending = (
                ConfluenceHit.query
                .filter_by(alert_sent=False)
                .filter(ConfluenceHit.created_at >= datetime.now(timezone.utc) - timedelta(days=14))
                .order_by(ConfluenceHit.created_at.desc())
                .limit(20)
                .all()
            )
            confluence_hits = [{
                "hit":          h,                       # kept so we can mark it sent
                "brand":        h.brand_name,
                "signal_types": h.get_signal_types(),
                "signal_count": h.signal_count,
                "score":        h.bullish_score,
                "level":        h.watch_level,
            } for h in _pending]
        except Exception as exc:
            logger.warning("Weekly digest: confluence queue read failed: %s", exc)

        people_matches = []
        try:
            _prows = Item.query.filter(
                Item.item_type == 'signal',
                Item.created_at >= week_ago,
                Item.description.contains('_match'),
            ).all()
            for _pi in _prows:
                try:
                    _pm = json.loads(_pi.description or "{}")
                except Exception:
                    continue
                _match = _pm.get("conviction_match") or _pm.get("exit_alumni_match")
                if not _match:
                    continue
                people_matches.append({
                    "person":      _match.get("name", "Unknown"),
                    "match_type":  "conviction" if _pm.get("conviction_match") else "alumni",
                    "brand":       _pm.get("company_name", _pi.title),
                    "signal_type": _pm.get("signal_type", ""),
                    "score":       (_pm.get("enrichment") or {}).get("bullish_score"),
                })
        except Exception as exc:
            logger.warning("Weekly digest: people-match read failed: %s", exc)

        if not hot_new and not warm_new and not confluence_hits and not people_matches:
            logger.info("Weekly digest: nothing new this week — skipping")
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

        # Backlog health, so the elevated-budget decision can be made on numbers
        # rather than vibes. "Unassessed" = saved but never scored OR triaged —
        # invisible to conviction matching, confluence and alerts, not merely
        # missing a score.
        try:
            unassessed = (Item.query
                          .filter(Item.item_type == "signal")
                          .filter(~Item.description.contains('"enrichment"'))
                          .filter(~Item.description.contains('"triage"'))
                          .count())
        except Exception as exc:
            logger.warning("Weekly digest: backlog count failed: %s", exc)
            unassessed = None

        budget_notice = None
        if _run_cap_reached():
            _backlog = ("unknown" if unassessed is None
                        else format(unassessed, ","))
            _weeks = max(1, (_date.today() - _RUN_CAP_SET_ON).days // 7)
            budget_notice = (
                f"The two-week run cap set on {_RUN_CAP_SET_ON:%b %d} is up "
                f"({_weeks} weeks in). This is a decision point, not a fault — "
                f"nothing has stopped.\n\n"
                f"Three questions, in order of how much they matter:\n"
                f"1. Has anyone opened a brand from these digests and acted on "
                f"it? If not, the problem is not the scoring.\n"
                f"2. Keep the scoring budget at {_BUDGET_TRIAL_TO:,} or drop "
                f"back to {_BUDGET_TRIAL_FROM:,}? Backlog now: {_backlog} "
                f"unassessed. (SCAN_ENRICH_BUDGET)\n"
                f"3. Is the weekly rhythm right, or should this go quiet and be "
                f"pulled on demand instead?\n\n"
                f"Deferred work is listed in docs/NEXT_LEVEL_BACKLOG.md — none "
                f"of it should start before these are answered."
            )
            logger.info("Weekly digest: two-week run cap reached — decision banner shown")

        week_label = datetime.now(timezone.utc).strftime("%b %d, %Y")
        sent_ok = False
        shown_confluence_n = None
        for addr in [e.strip() for e in alert_emails.split(",") if e.strip()]:
            try:
                _shown = send_weekly_digest_email(
                    addr, hot_signals[:5], warm_signals[:5], week_label,
                    confluence_hits=[
                        {k: v for k, v in h.items() if k != "hit"} for h in confluence_hits
                    ],
                    people_matches=people_matches,
                    backlog_unassessed=unassessed,
                    budget_notice=budget_notice,
                )
                # Retire only what was actually rendered (see _count_label).
                shown_confluence_n = min(shown_confluence_n, _shown) if shown_confluence_n is not None else _shown
                logger.info("Weekly digest sent to %s", addr)
                sent_ok = True
            except Exception as exc:
                logger.warning("Weekly digest email failed to %s: %s", addr, exc)

        # Drain the confluence queue — but only what was actually delivered.
        if sent_ok and confluence_hits:
            try:
                _now = datetime.now(timezone.utc)
                # Only the hits the email actually listed. The 2026-08-03 digest
                # counted 20 and showed 12, then retired all 20 — eight
                # triangulated brands were dequeued without ever being seen.
                _retire = confluence_hits[:shown_confluence_n or 0]
                if len(_retire) < len(confluence_hits):
                    logger.info(
                        "Weekly digest: %d confluence hits held back for next week",
                        len(confluence_hits) - len(_retire),
                    )
                for h in _retire:
                    h["hit"].alert_sent    = True
                    h["hit"].alert_sent_at = _now
                db.session.commit()
                logger.info("Weekly digest: marked %d confluence hits sent", len(_retire))
            except Exception as exc:
                db.session.rollback()
                logger.warning("Weekly digest: could not mark confluence hits sent: %s", exc)

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


def _publish_scheduler_liveness(app, scheduler) -> None:
    """
    Record that a scheduler is alive in this process and when each job next
    fires, so /api/admin/scheduler/status can answer "is anything scheduled?"
    without waiting for a completed run.

    The heartbeat only lands AFTER a full nightly run, so a scheduler that
    never fires is indistinguishable from one that is merely mid-run — which is
    exactly the ambiguity that let a dead scheduler go unnoticed on 2026-07-26.
    next_run_at removes it.
    """
    from ..models.item import Item
    from ..extensions import db

    try:
        jobs = {
            job.id: job.next_run_time.isoformat() if job.next_run_time else None
            for job in scheduler.get_jobs()
        }
    except Exception as exc:                      # never let telemetry break startup
        logger.warning("Scheduler liveness: could not read jobs: %s", exc)
        return

    payload = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "pid":        os.getpid(),
        "jobs":       jobs,
    }
    with app.app_context():
        try:
            owner_id = _system_owner_id()
            if owner_id is None:
                logger.error("Scheduler liveness: no user row to own the marker — skipping")
                return
            row = Item.query.filter_by(title="__scheduler_liveness__", item_type="system").first()
            if row:
                row.description = json.dumps(payload)
            else:
                db.session.add(Item(
                    title="__scheduler_liveness__", item_type="system",
                    owner_id=owner_id, description=json.dumps(payload),
                ))
            db.session.commit()
        except Exception as exc:
            logger.warning("Scheduler liveness write failed: %s", exc)


def start_scheduler(app):
    """Start the APScheduler background scheduler in EVERY gunicorn worker.

    Deduplication is the database's job, not this function's. Each job body
    calls _acquire_job_lock first, which takes a Postgres advisory lock and
    checks a TTL row, so exactly one worker executes a given job even though
    all of them schedule it.

    This deliberately replaced an fcntl file-lock that elected a single
    "scheduler owner" worker. That design had a silent single point of failure:
    the lock was held for the life of the PROCESS, but the scheduler is a
    daemon THREAD inside it. If the thread died while the process stayed
    healthy — still serving requests, still passing /health — the lock stayed
    held, no other worker would ever start a scheduler, and nothing ran again
    until the next deploy. Observed 2026-07-26: the app was fully healthy and
    the nightly scan simply never fired.

    The failure mode now is a worker dying, which gunicorn replaces
    automatically, and the replacement re-registers its own scheduler.
    """
    global _scheduler
    if _scheduler is not None:
        return  # Already running in this process

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
        _publish_scheduler_liveness(app, _scheduler)
        logger.info(
            "Bullish scheduler started — daily scan 11:00 UTC, weekly digest Mon 09:00 UTC, "
            "founder news Wed 08:00 UTC, press monitor Thu 08:00 UTC, "
            "inbox audit reminder 1st 07:00 UTC, watchlist headline sweep 1st 08:00 UTC, "
            "quarterly LinkedIn poll reminder Jan/Apr/Jul/Oct 1 09:00 UTC, "
            "quarterly Founder Radar poll reminder Jan/Apr/Jul/Oct 1 09:30 UTC"
        )
    except Exception as exc:
        logger.warning("Scheduler could not start: %s", exc)
