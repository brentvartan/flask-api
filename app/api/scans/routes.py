import hashlib
import json
import logging
import threading

from flask import jsonify, request, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity

from . import bp
from ...extensions import db, limiter


@bp.before_request
def _refuse_when_frozen():
    """
    FREEZE GATE for every manual scan route.

    Scheduled jobs are stopped at _acquire_job_lock; these POSTs bypass that
    entirely. Guarding the blueprint rather than each route is deliberate — a
    collector added later is covered automatically, whereas a per-route decorator
    is exactly the shape that gets forgotten.

    Collection itself costs nothing, but it feeds scoring and writes rows. A
    freeze means nothing fires.
    """
    from flask import jsonify as _jsonify
    from ...services.cost import is_frozen
    if request.method == "POST" and is_frozen():
        return _jsonify({
            "error": "frozen",
            "message": "Stealth Finder is frozen. Nothing is scheduled or scanning "
                       "and no paid calls are made. Unfreeze from Settings or "
                       "POST /api/admin/freeze with {\"frozen\": false}.",
        }), 409
from ...models.item import Item
from ...services.trademarks import search_recent_trademarks
from ...services.delaware import search_recent_delaware_entities
from ...services.producthunt import search_recent_producthunt
from ...services.app_store import search_recent_app_store
from ...services.newswire import search_recent_newswire
from ...services.ctlogs import search_recent_ct_domains
from ...services.press_stealth import search_recent_press_stealth
from ...services.signal_pipeline import process_saved_signal, resolve_alert_emails, _safe_title

logger = logging.getLogger(__name__)


def _spawn(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def _commit_and_spawn(new_items, user_id, signal_type):
    """Commit new signal items and spawn the shared post-save pipeline."""
    if not new_items:
        return
    db.session.commit()
    app = current_app._get_current_object()
    new_ids = [i.id for i in new_items]
    _spawn(_process_items_in_background, app, new_ids)
    # Confluence runs INSIDE the pipeline, sequentially, after enrichment — do not
    # re-add a parallel confluence spawn here. That race is what flooded the inbox
    # with COLD brands (2026-07-25): confluence always won and emailed with no score.


def _process_items_in_background(app, item_ids: list):
    """
    Background thread: run the shared post-save pipeline over newly saved signals
    from a manual scan.

    The pipeline (app/services/signal_pipeline.py) is the SAME one the nightly
    scan calls, so a manual scan and a scheduled scan do identical work. This
    function must stay a thin loop — every previous attempt to "just add one
    thing here" is why the two paths diverged.
    """
    with app.app_context():
        # Resolved once per batch rather than per signal.
        alert_emails = resolve_alert_emails()
        for item_id in item_ids:
            try:
                outcome = process_saved_signal(item_id, alert_emails=alert_emails)
                if outcome.get("errors"):
                    logger.warning(
                        "Signal pipeline degraded for item %s: %s",
                        item_id, "; ".join(outcome["errors"]),
                    )
            except Exception as exc:
                logger.warning("Signal pipeline failed for item %s: %s", item_id, exc)
            finally:
                # Blast radius of ONE signal, matching the scheduled path. This
                # loop shares a single app context — and therefore one session —
                # across the whole batch, so a signal that poisoned the session
                # would otherwise fail every signal behind it.
                db.session.remove()


def _make_fingerprint(signal_type: str, company_name: str, timestamp: str) -> str:
    """Stable 16-char hex fingerprint for a signal — used to prevent duplicates.

    Normalises company_name by uppercasing, stripping outer whitespace, and
    collapsing internal runs of whitespace to a single space so that
    'Foo  Bar' and 'Foo Bar' map to the same fingerprint.
    """
    import re as _re
    normalised = _re.sub(r'\s+', ' ', company_name.upper().strip())
    key = f"{signal_type}:{normalised}:{timestamp[:10]}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _load_existing_fps(user_id: int) -> set:
    """
    Return the set of fingerprint strings already stored for this user.

    Only items that have a 'fp' key in their JSON description are considered.
    Items created before dedup was introduced simply won't have 'fp', so the
    first scan after the upgrade may re-save a handful of very recent signals —
    but subsequent scans will be fully deduplicated.
    """
    # Pull every item that has at least a fingerprint field stored
    rows = (
        Item.query
        .filter_by(owner_id=user_id)
        .filter(Item.description.contains('"fp"'))
        .with_entities(Item.description)
        .all()
    )
    fps = set()
    for (desc,) in rows:
        try:
            obj = json.loads(desc or "{}")
            fp = obj.get("fp")
            if fp:
                fps.add(fp)
        except (json.JSONDecodeError, TypeError):
            pass
    return fps


@bp.route("/trademark", methods=["POST"])
@jwt_required()
@limiter.limit("10 per minute")
def run_trademark_scan():
    """
    Fetch real USPTO trademark filings and persist new ones for the current user.

    The route handles deduplication server-side using a per-signal fingerprint
    (sha256 of signal_type + company_name + filed_date) stored in each item's
    JSON description.  Running the scan multiple times is safe — only genuinely
    new filings are written to the database.

    Request body (all optional):
        days_back   int   Days of history to search (7–90, default 30)
        max_results int   USPTO results to fetch    (1–500, default 200)

    Response:
        {
            "total_found": int,   // total matches in USPTO for the date range
            "fetched":     int,   // results actually returned from USPTO
            "new_saved":   int,   // signals written to the database (new)
            "skipped":     int,   // signals skipped because they already exist
            "error":       null | str
        }
    """
    data = request.get_json(silent=True) or {}

    days_back = max(7, min(int(data.get("days_back", 30)), 90))
    max_results = max(1, min(int(data.get("max_results", 200)), 500))

    # ── 1. Fetch from USPTO ───────────────────────────────────────────────────
    result = search_recent_trademarks(days_back=days_back, max_results=max_results)

    if result.get("error"):
        return jsonify({
            "total_found": 0, "fetched": 0,
            "new_saved": 0,   "skipped": 0,
            "error": result["error"],
        }), 502

    signals = result["signals"]
    total_found = result["total_found"]

    # ── 2. Load existing fingerprints so we can skip duplicates ───────────────
    user_id = int(get_jwt_identity())
    existing_fps = _load_existing_fps(user_id)

    # ── 3. Persist only new signals ───────────────────────────────────────────
    new_items = []
    skipped = 0

    for sig in signals:
        fp = _make_fingerprint("trademark", sig["companyName"], sig["timestamp"])

        if fp in existing_fps:
            skipped += 1
            continue

        item = Item(
            title=_safe_title(sig["companyName"]),
            owner_id=user_id,
            item_type="signal",
            description=json.dumps({
                "_type":            "signal",
                "fp":               fp,
                "company_name":     sig["companyName"],
                "signal_type":      "trademark",
                "category":         sig["category"],
                "score_boost":      sig.get("score_boost", 5),
                "is_intent_to_use": sig.get("is_intent_to_use"),
                "description":      sig["description"],
                "url":              sig["url"],
                "notes":            sig.get("notes", ""),
                "brand_uncertain":  sig.get("brand_uncertain", False),
                # Owner is first-class on trademark signals (see trademarks.py) and
                # feeds person-key confluence, people matching and the enrichment
                # prompt. The scheduled path persists it too — keep them in step.
                "owner":            sig.get("owner"),
                "owner_is_person":  sig.get("owner_is_person", False),
                "timestamp":        sig["timestamp"],
            }, separators=(",", ":")),
        )
        db.session.add(item)
        new_items.append(item)
        existing_fps.add(fp)

    new_saved = len(new_items)
    _commit_and_spawn(new_items, user_id, "trademark")
    if new_saved > 0:
        logger.info("Trademark scan: %d new signals queued for enrichment + confluence check", new_saved)

    return jsonify({
        "total_found": total_found,
        "fetched":     len(signals),
        "new_saved":   new_saved,
        "skipped":     skipped,
        "error":       None,
    }), 200


@bp.route("/delaware", methods=["POST"])
@jwt_required()
@limiter.limit("10 per minute")
def run_delaware_scan():
    """
    Fetch recent Form D filings from SEC EDGAR and surface consumer brand signals.

    Request body (all optional):
        days_back    int   Days of filings to search (1–30, default 7)
        max_results  int   Max DE entities to process (1–300, default 150)

    Response:
        {
            "total_found": int,
            "fetched":     int,   // Form D signals returned
            "new_saved":   int,
            "skipped":     int,
            "error":       null | str
        }
    """
    data = request.get_json(silent=True) or {}

    days_back   = max(1, min(int(data.get("days_back",   7)),   30))
    max_results = max(1, min(int(data.get("max_results", 500)), 2000))

    # ── 1. Fetch Form D filings ───────────────────────────────────────────────
    result = search_recent_delaware_entities(
        days_back=days_back,
        max_results=max_results,
    )

    if result.get("error"):
        return jsonify({
            "total_found": 0, "fetched": 0,
            "new_saved": 0, "skipped": 0,
            "error": result["error"],
        }), 502

    signals     = result["signals"]
    total_found = result["total_found"]

    # ── 2. Dedup and persist ──────────────────────────────────────────────────
    user_id      = int(get_jwt_identity())
    existing_fps = _load_existing_fps(user_id)

    new_items = []
    skipped   = 0

    for sig in signals:
        fp = _make_fingerprint(sig["signal_type"], sig["companyName"], sig["timestamp"])

        if fp in existing_fps:
            skipped += 1
            continue

        item = Item(
            title=_safe_title(sig["companyName"]),
            owner_id=user_id,
            item_type="signal",
            description=json.dumps({
                "_type":        "signal",
                "fp":           fp,
                "company_name": sig["companyName"],
                "signal_type":  sig["signal_type"],
                "category":     sig["category"],
                "score_boost":  sig.get("score_boost", 5),
                "description":  sig["description"],
                "url":          sig["url"],
                "notes":        sig.get("notes", ""),
                "brand_uncertain":  sig.get("brand_uncertain", False),
                "timestamp":    sig["timestamp"],
                "_adsh":        sig.get("_adsh", ""),
                "_cik":         sig.get("_cik", ""),
                # Keep in step with the whitelist in scheduler.py::run_scan_now.
                # delaware.py already computed all of these during the scan;
                # dropping them here made the manual path lose the officer names,
                # the promoted conviction/alumni match and the raise size — the
                # exact scheduled-vs-manual divergence P1 exists to end. A
                # dropped exit_alumni_match also silently disarms the immediate
                # ALUMNI watchlist alert.
                "related_persons":    sig.get("related_persons") or [],
                "filer_name":         sig.get("filer_name"),
                "conviction_match":   sig.get("conviction_match"),
                "exit_alumni_match":  sig.get("exit_alumni_match"),
                "total_offering":     sig.get("total_offering"),
                "amount_sold":        sig.get("amount_sold"),
                "minimum_investment": sig.get("minimum_investment"),
            }, separators=(",", ":")),
        )
        db.session.add(item)
        new_items.append(item)
        existing_fps.add(fp)

    new_saved = len(new_items)
    _commit_and_spawn(new_items, user_id, "delaware")
    if new_saved > 0:
        logger.info("Delaware scan: %d new signals queued for enrichment + confluence check", new_saved)

    return jsonify({
        "total_found": total_found,
        "fetched":     result["fetched"],
        "new_saved":   new_saved,
        "skipped":     skipped,
        "error":       None,
    }), 200


@bp.route("/producthunt", methods=["POST"])
@jwt_required()
@limiter.limit("10 per minute")
def run_producthunt_scan():
    """
    Fetch recent Product Hunt consumer launches and persist new ones.

    Request body (all optional):
        days_back   int   Days of launches to include (1–30, default 14)
        max_results int   Max PH items to process    (1–200, default 100)

    Response:
        { "total_found": int, "fetched": int, "new_saved": int,
          "skipped": int, "error": null | str }
    """
    data = request.get_json(silent=True) or {}

    days_back   = max(1, min(int(data.get("days_back",   14)),  30))
    max_results = max(1, min(int(data.get("max_results", 100)), 200))

    result = search_recent_producthunt(days_back=days_back, max_results=max_results)

    if result.get("error"):
        return jsonify({
            "total_found": 0, "fetched": 0,
            "new_saved": 0, "skipped": 0,
            "error": result["error"],
        }), 502

    signals     = result["signals"]
    total_found = result["total_found"]

    user_id      = int(get_jwt_identity())
    existing_fps = _load_existing_fps(user_id)

    new_items = []
    skipped   = 0

    for sig in signals:
        fp = _make_fingerprint("producthunt", sig["companyName"], sig["timestamp"])

        if fp in existing_fps:
            skipped += 1
            continue

        item = Item(
            title=_safe_title(sig["companyName"]),
            owner_id=user_id,
            item_type="signal",
            description=json.dumps({
                "_type":        "signal",
                "fp":           fp,
                "company_name": sig["companyName"],
                "signal_type":  "producthunt",
                "category":     sig["category"],
                "score_boost":  sig.get("score_boost", 8),
                "description":  sig["description"],
                "url":          sig["url"],
                "notes":        sig.get("notes", ""),
                "brand_uncertain":  sig.get("brand_uncertain", False),
                "timestamp":    sig["timestamp"],
            }, separators=(",", ":")),
        )
        db.session.add(item)
        new_items.append(item)
        existing_fps.add(fp)

    new_saved = len(new_items)
    _commit_and_spawn(new_items, user_id, "producthunt")
    if new_saved > 0:
        logger.info("ProductHunt scan: %d new signals queued for enrichment + confluence check", new_saved)

    return jsonify({
        "total_found": total_found,
        "fetched":     result["fetched"],
        "new_saved":   new_saved,
        "skipped":     skipped,
        "error":       None,
    }), 200


@bp.route("/app-store", methods=["POST"])
@jwt_required()
@limiter.limit("10 per minute")
def run_app_store_scan():
    """
    Scan the App Store for recent consumer app launches.

    Uses the iTunes Search API (free, no API key required).
    Surfaces consumer apps updated within the last N days across
    Health/Wellness, Beauty, CPG/Food/Drink, Fitness, and related categories.

    Request body (all optional):
        days_back   int   Days of activity to surface (7–90, default 30)
        max_results int   Max results to process (1–200, default 100)
    """
    data        = request.get_json(silent=True) or {}
    user_id     = int(get_jwt_identity())
    days_back   = max(7, min(int(data.get("days_back",   30)), 90))
    max_results = max(1, min(int(data.get("max_results", 100)), 200))

    result = search_recent_app_store(days_back=days_back, max_results=max_results)

    if result.get("error"):
        return jsonify({
            "total_found": 0, "fetched": 0,
            "new_saved": 0,   "skipped": 0,
            "error": result["error"],
        }), 502

    signals = result.get("signals", [])
    if not signals:
        return jsonify({"total_found": 0, "fetched": 0, "new_saved": 0, "skipped": 0, "error": None}), 200

    existing_fps = _load_existing_fps(user_id)
    new_items = []
    skipped   = 0

    for sig in signals:
        fp = _make_fingerprint("app_store", sig["companyName"], str(sig.get("app_id", "")))

        if fp in existing_fps:
            skipped += 1
            continue

        item = Item(
            title=_safe_title(sig["companyName"]),
            owner_id=user_id,
            item_type="signal",
            description=json.dumps({
                "_type":        "signal",
                "fp":           fp,
                "company_name": sig["companyName"],
                "signal_type":  "app_store",
                "category":     sig["category"],
                "score_boost":  sig.get("score_boost", 8),
                "description":  sig["description"],
                "url":          sig["url"],
                "notes":        sig.get("notes", ""),
                "brand_uncertain":  sig.get("brand_uncertain", False),
                "timestamp":    sig["timestamp"],
                "app_id":       sig.get("app_id"),
                "developer":    sig.get("developer", ""),
                "rating":       sig.get("rating", 0),
                "rating_count": sig.get("rating_count", 0),
                "icon_url":     sig.get("icon_url", ""),
            }, separators=(",", ":")),
        )
        db.session.add(item)
        new_items.append(item)
        existing_fps.add(fp)

    new_saved = len(new_items)
    _commit_and_spawn(new_items, user_id, "app_store")
    if new_saved > 0:
        logger.info("App Store scan: %d new signals queued for enrichment", new_saved)

    return jsonify({
        "total_found": result["total_found"],
        "fetched":     len(signals),
        "new_saved":   new_saved,
        "skipped":     skipped,
        "error":       None,
    }), 200


@bp.route("/newswire", methods=["POST"])
@jwt_required()
@limiter.limit("10 per minute")
def run_newswire_scan():
    """
    Scan PR Newswire and BusinessWire RSS feeds for consumer brand press releases.

    Surfaces brands that have just broken stealth — seed announces, product launches,
    and funding rounds — before they get wide press coverage. Newswire signals combined
    with trademark/Delaware filings are the strongest 'just-before-VC' indicator.

    Request body (all optional):
        days_back   int   Days of releases to include (1–30, default 14)
        max_results int   Max results to process (1–200, default 100)
    """
    data        = request.get_json(silent=True) or {}
    user_id     = int(get_jwt_identity())
    days_back   = max(1, min(int(data.get("days_back",   14)),  30))
    max_results = max(1, min(int(data.get("max_results", 100)), 200))

    result = search_recent_newswire(days_back=days_back, max_results=max_results)

    if result.get("error") and not result.get("signals"):
        # All feeds failed — return 200 with error flag so the frontend scan
        # continues to the next source rather than aborting the whole run.
        return jsonify({
            "total_found": 0, "fetched": 0,
            "new_saved": 0,   "skipped": 0,
            "error": result["error"],
        }), 200

    signals = result.get("signals", [])
    if not signals:
        return jsonify({"total_found": 0, "fetched": 0, "new_saved": 0, "skipped": 0, "error": None}), 200

    existing_fps = _load_existing_fps(user_id)
    new_items = []
    skipped   = 0

    for sig in signals:
        fp = _make_fingerprint("newswire", sig["companyName"], sig["timestamp"])

        if fp in existing_fps:
            skipped += 1
            continue

        item = Item(
            title=_safe_title(sig["companyName"]),
            owner_id=user_id,
            item_type="signal",
            description=json.dumps({
                "_type":        "signal",
                "fp":           fp,
                "company_name": sig["companyName"],
                "signal_type":  "newswire",
                "category":     sig["category"],
                "score_boost":  sig.get("score_boost", 8),
                "description":  sig["description"],
                "url":          sig["url"],
                "notes":        sig.get("notes", ""),
                "brand_uncertain":  sig.get("brand_uncertain", False),
                "timestamp":    sig["timestamp"],
            }, separators=(",", ":")),
        )
        db.session.add(item)
        new_items.append(item)
        existing_fps.add(fp)

    new_saved = len(new_items)
    _commit_and_spawn(new_items, user_id, "newswire")
    if new_saved > 0:
        logger.info("Newswire scan: %d new signals queued for enrichment + confluence check", new_saved)

    return jsonify({
        "total_found": len(signals),
        "fetched":     len(signals),
        "new_saved":   new_saved,
        "skipped":     skipped,
        "error":       result.get("error"),
    }), 200


@bp.route("/ctlogs", methods=["POST"])
@jwt_required()
@limiter.limit("10 per minute")
def run_ctlogs_scan():
    """
    Scan Certificate Transparency logs for new consumer-brand domain registrations.

    Surfaces brands at domain-registration time — typically 6-24 months before
    press coverage. The board.fun case (Board by ex-Mirror founder) was detectable
    22 months early via CT logs.

    Request body (all optional):
        days_back   int   Days of CT records to include (1–30, default 14)
        max_results int   Max results to process (1–100, default 50)
    """
    data        = request.get_json(silent=True) or {}
    user_id     = int(get_jwt_identity())
    days_back   = max(1, min(int(data.get("days_back",   14)),  30))
    max_results = max(1, min(int(data.get("max_results", 50)),  100))

    result = search_recent_ct_domains(days_back=days_back, max_results=max_results)

    if result.get("error") and not result.get("signals"):
        return jsonify({
            "total_found": 0, "fetched": 0,
            "new_saved": 0,   "skipped": 0,
            "error": result["error"],
        }), 502

    signals = result.get("signals", [])
    if not signals:
        return jsonify({"total_found": 0, "fetched": 0, "new_saved": 0, "skipped": 0, "error": None}), 200

    existing_fps = _load_existing_fps(user_id)
    new_items = []
    skipped   = 0

    for sig in signals:
        fp = _make_fingerprint("domain_ct", sig["companyName"], sig["timestamp"])

        if fp in existing_fps:
            skipped += 1
            continue

        item = Item(
            title=_safe_title(sig["companyName"]),
            owner_id=user_id,
            item_type="signal",
            description=json.dumps({
                "_type":        "signal",
                "fp":           fp,
                "company_name": sig["companyName"],
                "signal_type":  "domain_ct",
                "category":     sig["category"],
                "score_boost":  sig.get("score_boost", 6),
                "description":  sig["description"],
                "url":          sig["url"],
                "notes":        sig.get("notes", ""),
                "brand_uncertain":  sig.get("brand_uncertain", False),
                "timestamp":    sig["timestamp"],
            }, separators=(",", ":")),
        )
        db.session.add(item)
        new_items.append(item)
        existing_fps.add(fp)

    new_saved = len(new_items)
    _commit_and_spawn(new_items, user_id, "domain_ct")
    if new_saved > 0:
        logger.info("CT logs scan: %d new domain signals queued for enrichment", new_saved)

    return jsonify({
        "total_found": result.get("total_found", len(signals)),
        "fetched":     len(signals),
        "new_saved":   new_saved,
        "skipped":     skipped,
        "error":       result.get("error"),
    }), 200


@bp.route("/press-stealth", methods=["POST"])
@jwt_required()
@limiter.limit("10 per minute")
def run_press_stealth_scan():
    """
    Scan startup + consumer trade press RSS for stealth-founder language.

    Surfaces journalist-written articles where a founder is described as
    "building something new," "quietly building," "left [BigCo] to build," etc.
    Pattern-keyed (not person-keyed) — catches the Board/Brynn Putnam type
    19 months before the Series A announcement.

    When a conviction founder's name appears in the article, the conviction
    check in the shared post-save pipeline will flag it automatically.

    Request body (all optional):
        days_back   int   Days of articles to include (1–30, default 14)
        max_results int   Max results to process (1–100, default 50)
    """
    data        = request.get_json(silent=True) or {}
    user_id     = int(get_jwt_identity())
    days_back   = max(1, min(int(data.get("days_back",   14)),  30))
    max_results = max(1, min(int(data.get("max_results", 50)),  100))

    result = search_recent_press_stealth(days_back=days_back, max_results=max_results)

    if result.get("error") and not result.get("signals"):
        return jsonify({
            "total_found": 0, "fetched": 0,
            "new_saved": 0,   "skipped": 0,
            "error": result["error"],
        }), 502

    signals = result.get("signals", [])
    if not signals:
        return jsonify({"total_found": 0, "fetched": 0, "new_saved": 0, "skipped": 0, "error": None}), 200

    existing_fps = _load_existing_fps(user_id)
    new_items = []
    skipped   = 0

    for sig in signals:
        fp = _make_fingerprint("press_stealth", sig["companyName"], sig["timestamp"])

        if fp in existing_fps:
            skipped += 1
            continue

        item = Item(
            title=_safe_title(sig["companyName"]),
            owner_id=user_id,
            item_type="signal",
            description=json.dumps({
                "_type":        "signal",
                "fp":           fp,
                "company_name": sig["companyName"],
                "signal_type":  "press_stealth",
                "category":     sig["category"],
                "score_boost":  sig.get("score_boost", 10),
                "description":  sig["description"],
                "url":          sig["url"],
                "notes":        sig.get("notes", ""),
                "brand_uncertain":  sig.get("brand_uncertain", False),
                "timestamp":    sig["timestamp"],
                "source":       sig.get("source", ""),
            }, separators=(",", ":")),
        )
        db.session.add(item)
        new_items.append(item)
        existing_fps.add(fp)

    new_saved = len(new_items)
    _commit_and_spawn(new_items, user_id, "press_stealth")
    if new_saved > 0:
        logger.info("Press stealth scan: %d articles queued for enrichment + conviction check", new_saved)

    return jsonify({
        "total_found": result.get("total_found", len(signals)),
        "fetched":     len(signals),
        "new_saved":   new_saved,
        "skipped":     skipped,
        "error":       result.get("error"),
    }), 200
