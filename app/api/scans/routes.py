import hashlib
import json
import logging
import os
import threading

from flask import jsonify, request, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity

from . import bp
from ...extensions import db, limiter
from ...models.item import Item
from ...services.trademarks import search_recent_trademarks
from ...services.delaware import search_recent_delaware_entities
from ...services.producthunt import search_recent_producthunt

logger = logging.getLogger(__name__)


def _get_alert_emails() -> list:
    """Return the list of alert email addresses from DB settings or env var."""
    alert_emails_str = os.environ.get("ALERT_EMAILS", "").strip()
    try:
        settings_item = Item.query.filter_by(title="__bullish_settings__").first()
        if settings_item:
            settings = json.loads(settings_item.description or "{}")
            emails = settings.get("alert_emails", [])
            if emails:
                return [e.strip() for e in emails if e.strip()]
    except Exception:
        pass
    return [e.strip() for e in alert_emails_str.split(",") if e.strip()]


def _check_confluence_in_background(app, item_id: int, owner_id: int,
                                     brand_name: str, signal_type: str,
                                     source_url: str = None):
    """Background thread: record signal event and fire confluence alert if triggered."""
    from ...services.confluence import record_signal_and_check_confluence, send_confluence_alert_for_hit

    with app.app_context():
        try:
            result = record_signal_and_check_confluence(
                item_id=item_id,
                owner_id=owner_id,
                brand_name=brand_name,
                signal_type=signal_type,
                source_url=source_url,
            )
            if result["is_confluence"]:
                alert_emails = _get_alert_emails()
                if alert_emails and result.get("hit_id"):
                    send_confluence_alert_for_hit(result["hit_id"], alert_emails)
                    logger.info("Confluence alert sent for %s (%d signals)", brand_name, result["signal_count"])
        except Exception as exc:
            logger.warning("Confluence check failed for item %s: %s", item_id, exc)


def _enrich_items_in_background(app, item_ids: list):
    """
    Background thread: enrich newly saved signals with Bullish AI immediately
    after a manual scan completes. Mirrors what run_scan_now() does for scheduled scans.
    """
    from ...services.enrichment import enrich_signal
    from ...services.founder_enrichment import run_founder_enrichment_in_background

    with app.app_context():
        for item_id in item_ids:
            try:
                item = db.session.get(Item, item_id)
                if not item:
                    continue
                meta = json.loads(item.description or "{}")
                if meta.get("_type") != "signal":
                    continue

                # Extract owner name from USPTO notes if present
                notes = meta.get("notes", "")
                owner = ""
                if notes.lower().startswith("owner:"):
                    part = notes[6:].strip()
                    dot = part.find(". ")
                    owner = part[:dot].strip() if dot > 0 else part.strip()

                enrichment = enrich_signal({
                    "companyName": meta.get("company_name", item.title),
                    "category":    meta.get("category", ""),
                    "signal_type": meta.get("signal_type", "trademark"),
                    "description": meta.get("description", ""),
                    "notes":       notes,
                    "owner":       owner,
                })

                if enrichment.get("enriched"):
                    meta["enrichment"] = enrichment
                    item.description = json.dumps(meta, separators=(",", ":"))
                    db.session.commit()
                    logger.info(
                        "Auto-enriched %s (item %s) → %s / score %s",
                        meta.get("company_name"), item_id,
                        enrichment.get("watch_level"), enrichment.get("bullish_score"),
                    )

                    # Spawn founder enrichment for HOT brands with no founder identified yet
                    if (
                        enrichment.get("bullish_score", 0) >= 70
                        and enrichment.get("founder", {}).get("confidence") == "unknown"
                    ):
                        alert_emails = _get_alert_emails()
                        brand_name = meta.get("company_name", item.title)
                        category = meta.get("category", "")
                        one_line_thesis = enrichment.get("one_line_thesis", "")
                        filer_name = owner or None
                        logger.info(
                            "Triggering founder enrichment for HOT brand '%s' (item %s)",
                            brand_name, item_id,
                        )
                        run_founder_enrichment_in_background(
                            app=app,
                            item_id=item_id,
                            brand_name=brand_name,
                            category=category,
                            one_line_thesis=one_line_thesis,
                            filer_name=filer_name,
                            alert_emails=alert_emails,
                        )

                    # Auto-add HOT brands to watchlist
                    if enrichment.get("watch_level") == "hot":
                        try:
                            from ...services.watchlist import auto_add_to_watchlist
                            _brand_name = meta.get("company_name", item.title)
                            _founder = enrichment.get("founder", {})
                            _sig_type = meta.get("signal_type", "trademark")
                            _sig_types = [_sig_type] if isinstance(_sig_type, str) else [_sig_type]
                            _user_id = item.owner_id
                            threading.Thread(
                                target=auto_add_to_watchlist,
                                args=(
                                    app.app_context(),
                                    _user_id,
                                    _brand_name,
                                    item_id,
                                    enrichment.get("bullish_score"),
                                    _founder.get("name") if _founder.get("confidence") != "unknown" else None,
                                    (enrichment.get("founder_score") or {}).get("total"),
                                    _founder.get("linkedin_url"),
                                    enrichment.get("one_line_thesis", ""),
                                    enrichment.get("cultural_theme", ""),
                                    _sig_types,
                                ),
                                daemon=True,
                            ).start()
                        except Exception as _wl_exc:
                            logger.warning("Auto-watchlist trigger failed for item %s: %s", item_id, _wl_exc)

            except Exception as exc:
                logger.warning("Background enrichment failed for item %s: %s", item_id, exc)


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
            title=sig["companyName"],
            owner_id=user_id,
            item_type="signal",
            description=json.dumps({
                "_type":        "signal",
                "fp":           fp,
                "company_name": sig["companyName"],
                "signal_type":  "trademark",
                "category":     sig["category"],
                "score_boost":  15,
                "description":  sig["description"],
                "url":          sig["url"],
                "notes":        sig.get("notes", ""),
                "timestamp":    sig["timestamp"],
            }, separators=(",", ":")),
        )
        db.session.add(item)
        new_items.append(item)
        existing_fps.add(fp)

    new_saved = len(new_items)
    if new_saved > 0:
        db.session.commit()
        new_ids = [item.id for item in new_items]
        app = current_app._get_current_object()
        # Auto-enrich
        threading.Thread(target=_enrich_items_in_background, args=(app, new_ids), daemon=True).start()
        # Confluence detection — check each new signal against the brand timeline
        for item in new_items:
            threading.Thread(
                target=_check_confluence_in_background,
                args=(app, item.id, user_id, item.title, "trademark", None),
                daemon=True,
            ).start()
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
    Fetch real Delaware LLC/Corp filings and cross-reference matching domains.

    Request body (all optional):
        days_back    int   Days of filings to search (1–30, default 7)
        max_results  int   Max DE entities to process (1–300, default 150)

    Response:
        {
            "total_found": int,
            "fetched":     int,   // DE entities returned
            "domain_hits": int,   // companion domain signals added
            "new_saved":   int,
            "skipped":     int,
            "error":       null | str
        }
    """
    data = request.get_json(silent=True) or {}

    days_back   = max(1, min(int(data.get("days_back",   7)),   30))
    max_results = max(1, min(int(data.get("max_results", 150)), 300))

    # ── 1. Fetch from OpenCorporates + domain cross-reference ─────────────────
    result = search_recent_delaware_entities(
        days_back=days_back,
        max_results=max_results,
        check_domains=True,
    )

    if result.get("error"):
        return jsonify({
            "total_found": 0, "fetched": 0,
            "domain_hits": 0, "new_saved": 0, "skipped": 0,
            "error": result["error"],
        }), 502

    signals     = result["signals"]
    total_found = result["total_found"]
    domain_hits = result["domain_hits"]

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
            title=sig["companyName"],
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
                "timestamp":    sig["timestamp"],
            }, separators=(",", ":")),
        )
        db.session.add(item)
        new_items.append(item)
        existing_fps.add(fp)

    new_saved = len(new_items)
    if new_saved > 0:
        db.session.commit()
        new_ids = [item.id for item in new_items]
        app = current_app._get_current_object()
        threading.Thread(target=_enrich_items_in_background, args=(app, new_ids), daemon=True).start()
        for item in new_items:
            meta = json.loads(item.description or "{}")
            threading.Thread(
                target=_check_confluence_in_background,
                args=(app, item.id, user_id, item.title, meta.get("signal_type", "delaware"), meta.get("url")),
                daemon=True,
            ).start()
        logger.info("Delaware scan: %d new signals queued for enrichment + confluence check", new_saved)

    return jsonify({
        "total_found": total_found,
        "fetched":     result["fetched"],
        "domain_hits": domain_hits,
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
            title=sig["companyName"],
            owner_id=user_id,
            item_type="signal",
            description=json.dumps({
                "_type":        "signal",
                "fp":           fp,
                "company_name": sig["companyName"],
                "signal_type":  "producthunt",
                "category":     sig["category"],
                "score_boost":  8,
                "description":  sig["description"],
                "url":          sig["url"],
                "notes":        sig.get("notes", ""),
                "timestamp":    sig["timestamp"],
            }, separators=(",", ":")),
        )
        db.session.add(item)
        new_items.append(item)
        existing_fps.add(fp)

    new_saved = len(new_items)
    if new_saved > 0:
        db.session.commit()
        new_ids = [item.id for item in new_items]
        app = current_app._get_current_object()
        threading.Thread(target=_enrich_items_in_background, args=(app, new_ids), daemon=True).start()
        for item in new_items:
            meta = json.loads(item.description or "{}")
            threading.Thread(
                target=_check_confluence_in_background,
                args=(app, item.id, user_id, item.title, "producthunt", meta.get("url")),
                daemon=True,
            ).start()
        logger.info("ProductHunt scan: %d new signals queued for enrichment + confluence check", new_saved)

    return jsonify({
        "total_found": total_found,
        "fetched":     result["fetched"],
        "new_saved":   new_saved,
        "skipped":     skipped,
        "error":       None,
    }), 200
