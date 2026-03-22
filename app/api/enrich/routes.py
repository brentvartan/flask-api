import json

from flask import jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity

from . import bp
from ...extensions import db
from ...models.item import Item
from ...services.enrichment import enrich_signal


def _parse_meta(item):
    """Return parsed description JSON or None if invalid/not a signal."""
    try:
        meta = json.loads(item.description or "{}")
        if meta.get("_type") == "signal":
            return meta
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def _extract_owner(notes: str) -> str:
    """Extract 'Owner: NAME' from USPTO notes field."""
    if not notes:
        return ""
    s = notes.strip()
    if s.lower().startswith("owner:"):
        owner_part = s[6:].strip()
        dot_idx = owner_part.find(". ")
        return owner_part[:dot_idx].strip() if dot_idx > 0 else owner_part.strip()
    return ""


@bp.route("/signal/<int:item_id>", methods=["POST"])
@jwt_required()
def enrich_single(item_id):
    """Enrich a single signal item with Bullish AI analysis."""
    user_id = int(get_jwt_identity())
    item = db.session.get(Item, item_id)

    if not item:
        return jsonify({"error": "Item not found"}), 404
    if item.owner_id != user_id:
        return jsonify({"error": "Forbidden"}), 403

    meta = _parse_meta(item)
    if meta is None:
        return jsonify({"error": "Item is not a signal"}), 422

    enrichment = enrich_signal({
        "companyName":  meta.get("company_name", item.title),
        "category":     meta.get("category", ""),
        "signal_type":  meta.get("signal_type", "trademark"),
        "description":  meta.get("description", ""),
        "notes":        meta.get("notes", ""),
        "owner":        _extract_owner(meta.get("notes", "")),
    })

    meta["enrichment"] = enrichment
    item.description = json.dumps(meta, separators=(",", ":"))
    db.session.commit()

    return jsonify({"enrichment": enrichment, "item_id": item_id}), 200


@bp.route("/batch", methods=["POST"])
@jwt_required()
def enrich_batch():
    """
    Enrich multiple signal items with Bullish AI analysis.

    Request body (all optional):
        item_ids        [int]  Specific item IDs to enrich
        unenriched_only bool   If true, enrich all signals that haven't been enriched yet
        limit           int    Max signals to enrich in this call (default 20, max 50)
    """
    user_id = int(get_jwt_identity())
    data = request.get_json(silent=True) or {}

    limit = min(int(data.get("limit", 20)), 50)

    if data.get("unenriched_only"):
        # Get all signal items for this user without enrichment data
        rows = (
            Item.query
            .filter_by(owner_id=user_id)
            .filter(Item.description.contains('"_type":"signal"'))
            .filter(~Item.description.contains('"enrichment"'))
            .order_by(Item.created_at.desc())
            .limit(limit)
            .all()
        )
    elif data.get("rescore_all"):
        # Re-score the most recent N signals regardless of existing enrichment
        rows = (
            Item.query
            .filter_by(owner_id=user_id)
            .filter(Item.description.contains('"_type":"signal"'))
            .order_by(Item.created_at.desc())
            .limit(limit)
            .all()
        )
    else:
        item_ids = data.get("item_ids", [])
        if not item_ids:
            return jsonify({"error": "Provide item_ids, set unenriched_only: true, or set rescore_all: true"}), 400
        rows = (
            Item.query
            .filter(Item.id.in_(item_ids[:limit]), Item.owner_id == user_id)
            .all()
        )

    enriched_count = 0
    error_count = 0

    for item in rows:
        meta = _parse_meta(item)
        if meta is None:
            continue

        result = enrich_signal({
            "companyName":  meta.get("company_name", item.title),
            "category":     meta.get("category", ""),
            "signal_type":  meta.get("signal_type", "trademark"),
            "description":  meta.get("description", ""),
            "notes":        meta.get("notes", ""),
            "owner":        _extract_owner(meta.get("notes", "")),
        })

        if result.get("enriched"):
            meta["enrichment"] = result
            item.description = json.dumps(meta, separators=(",", ":"))
            enriched_count += 1
        else:
            error_count += 1

    if enriched_count > 0:
        db.session.commit()

    return jsonify({
        "enriched":        enriched_count,
        "errors":          error_count,
        "total_processed": len(rows),
    }), 200
