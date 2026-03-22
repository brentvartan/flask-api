import hashlib
import json

from flask import jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity

from . import bp
from ...extensions import db
from ...models.item import Item
from ...services.trademarks import search_recent_trademarks
from ...services.delaware import search_recent_delaware_entities
from ...services.producthunt import search_recent_producthunt


def _make_fingerprint(signal_type: str, company_name: str, timestamp: str) -> str:
    """Stable 16-char hex fingerprint for a signal — used to prevent duplicates."""
    key = f"{signal_type}:{company_name.upper().strip()}:{timestamp[:10]}"
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
    new_saved = 0
    skipped = 0

    for sig in signals:
        fp = _make_fingerprint("trademark", sig["companyName"], sig["timestamp"])

        if fp in existing_fps:
            skipped += 1
            continue

        description = json.dumps({
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
        }, separators=(",", ":"))   # compact — matches JS JSON.stringify output

        db.session.add(Item(
            title=sig["companyName"],
            description=description,
            owner_id=user_id,
        ))
        existing_fps.add(fp)   # guard against duplicates within this batch
        new_saved += 1

    if new_saved > 0:
        db.session.commit()

    return jsonify({
        "total_found": total_found,
        "fetched":     len(signals),
        "new_saved":   new_saved,
        "skipped":     skipped,
        "error":       None,
    }), 200


@bp.route("/delaware", methods=["POST"])
@jwt_required()
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

    new_saved = 0
    skipped   = 0

    for sig in signals:
        fp = _make_fingerprint(sig["signal_type"], sig["companyName"], sig["timestamp"])

        if fp in existing_fps:
            skipped += 1
            continue

        description = json.dumps({
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
        }, separators=(",", ":"))

        db.session.add(Item(
            title=sig["companyName"],
            description=description,
            owner_id=user_id,
        ))
        existing_fps.add(fp)
        new_saved += 1

    if new_saved > 0:
        db.session.commit()

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

    new_saved = 0
    skipped   = 0

    for sig in signals:
        fp = _make_fingerprint("producthunt", sig["companyName"], sig["timestamp"])

        if fp in existing_fps:
            skipped += 1
            continue

        description = json.dumps({
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
        }, separators=(",", ":"))

        db.session.add(Item(
            title=sig["companyName"],
            description=description,
            owner_id=user_id,
        ))
        existing_fps.add(fp)
        new_saved += 1

    if new_saved > 0:
        db.session.commit()

    return jsonify({
        "total_found": total_found,
        "fetched":     result["fetched"],
        "new_saved":   new_saved,
        "skipped":     skipped,
        "error":       None,
    }), 200
