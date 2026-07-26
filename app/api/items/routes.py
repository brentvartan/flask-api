import logging

from flask import request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from marshmallow import ValidationError
from . import bp
from ...extensions import db
from ...models.item import Item
from ...models.user import User
from ...schemas import ItemSchema, ItemUpdateSchema, PaginationSchema

logger = logging.getLogger(__name__)

item_schema = ItemSchema()
item_update_schema = ItemUpdateSchema()
pagination_schema = PaginationSchema()

# Internal control rows live in the items table but are NOT user content:
#   __bullish_settings__     — holds slack_webhook_url and the alert_emails list
#   __jlock_<job_id>__       — job locks that gate the nightly scan
#   __scheduler_heartbeat__  — scheduler liveness marker
#
# Signal rows are intentionally shared across the whole team (see list_items and
# tests/test_items.py::test_update_other_users_item), so this API is deliberately
# NOT owner-scoped. That sharing must not extend to the control rows: without this
# guard any authenticated user could read the Slack webhook and alert emails via
# GET /api/items, or rewrite them via PUT /api/items/<id> — bypassing both the
# admin check and the field whitelist in the settings blueprint, which is the only
# sanctioned door to this data. Through this API the control rows do not exist.
#
# Matched two ways: the reserved `__name__` title namespace, and the item_type
# the scheduler stamps on its own rows, so a future control row that forgets the
# prefix is still covered.
_INTERNAL_TITLE_PREFIX = "__"
_INTERNAL_ITEM_TYPES = ("system", "settings")


def _is_internal(title) -> bool:
    """True for the reserved `__name__` title namespace.

    Every singleton lookup in the codebase is `filter_by(title=...).first()` with
    no tiebreak, so a second row with the same title could win the lookup — the
    same alert hijack by a different route. Reserved titles are server-side only.
    """
    return bool(title) and title.startswith(_INTERNAL_TITLE_PREFIX)


def _is_internal_item(item) -> bool:
    """True for a control row, by title or by item_type."""
    return _is_internal(item.title) or item.item_type in _INTERNAL_ITEM_TYPES


@bp.route("", methods=["GET"])
@jwt_required()
def list_items():
    """List items — returns only the current user's items, paginated.
    ---
    tags: [Items]
    security:
      - Bearer: []
    parameters:
      - in: query
        name: page
        schema: {type: integer, default: 1}
      - in: query
        name: per_page
        schema: {type: integer, default: 20}
    responses:
      200:
        description: Paginated list of items
    """
    try:
        params = pagination_schema.load(request.args)
    except ValidationError as e:
        return jsonify({"error": e.messages}), 422

    page = params["page"]
    per_page = params["per_page"]

    # Signal items are shared across all authenticated team members by design;
    # internal control rows are excluded (see _is_internal).
    pagination = (
        Item.query
        .filter(~Item.title.startswith(_INTERNAL_TITLE_PREFIX, autoescape=True))
        .order_by(Item.created_at.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )

    return jsonify({
        "items": [i.to_dict() for i in pagination.items],
        "pagination": {
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
            "pages": pagination.pages,
            "has_next": pagination.has_next,
            "has_prev": pagination.has_prev,
        },
    }), 200


@bp.route("", methods=["POST"])
@jwt_required()
def create_item():
    """Create a new item.
    ---
    tags: [Items]
    security:
      - Bearer: []
    requestBody:
      required: true
      content:
        application/json:
          schema:
            required: [title]
            properties:
              title: {type: string}
              description: {type: string}
    responses:
      201:
        description: Item created
    """
    try:
        data = item_schema.load(request.get_json() or {})
    except ValidationError as e:
        return jsonify({"error": e.messages}), 422

    user_id = int(get_jwt_identity())

    # A row that shadows a singleton title is as good as editing it — block both.
    if _is_internal(data["title"]):
        logger.warning(
            "Blocked create of reserved-title item %r by user %s", data["title"], user_id
        )
        return jsonify({"error": "Titles beginning with '__' are reserved for system records"}), 403

    # Derive item_type from JSON description if present (watchlist, signal, etc.)
    raw_desc = data.get("description") or ""
    try:
        import json as _json
        _parsed_type = _json.loads(raw_desc).get("_type") if raw_desc else None
    except Exception:
        _parsed_type = None
    # _type is caller-controlled, so it must not be able to mint a control row:
    # an item_type of "system"/"settings" would make the row internal — invisible
    # to list/get and undeletable through this API by anyone, including admins.
    if _parsed_type in _INTERNAL_ITEM_TYPES:
        logger.warning(
            "Blocked create of item with reserved _type %r by user %s", _parsed_type, user_id
        )
        return jsonify({"error": f"'{_parsed_type}' is a reserved record type"}), 403

    item = Item(title=data["title"], description=raw_desc, item_type=_parsed_type, owner_id=user_id)
    db.session.add(item)
    db.session.commit()
    return jsonify({"item": item.to_dict()}), 201


@bp.route("/<int:item_id>", methods=["GET"])
@jwt_required()
def get_item(item_id):
    """Get a single item.
    ---
    tags: [Items]
    security:
      - Bearer: []
    parameters:
      - in: path
        name: item_id
        required: true
        schema: {type: integer}
    responses:
      200:
        description: Item detail
      404:
        description: Not found
    """
    item = db.session.get(Item, item_id)
    if not item or _is_internal_item(item):
        return jsonify({"error": "Item not found"}), 404
    return jsonify({"item": item.to_dict()}), 200


@bp.route("/<int:item_id>", methods=["PUT"])
@jwt_required()
def update_item(item_id):
    """Update an item (owner or admin; system singletons are read-only).
    ---
    tags: [Items]
    security:
      - Bearer: []
    parameters:
      - in: path
        name: item_id
        required: true
        schema: {type: integer}
    responses:
      200:
        description: Updated item
    """
    try:
        data = item_update_schema.load(request.get_json() or {})
    except ValidationError as e:
        return jsonify({"error": e.messages}), 422

    user_id = int(get_jwt_identity())
    user = db.session.get(User, user_id)
    item = db.session.get(Item, item_id)
    if not item or _is_internal_item(item):
        # 404, not 403: through this API the control rows simply do not exist,
        # so a probe cannot even confirm one is there.
        if item is not None:
            logger.warning(
                "Blocked write to internal row %s (%r) by user %s",
                item.id, item.title, user_id,
            )
        return jsonify({"error": "Item not found"}), 404

    # Guard the INCOMING title too, not just the existing row. Otherwise the
    # namespace is bypassable by rename: create an innocuous row, then PUT it a
    # title of "__bullish_settings__" and you have a second settings row. Every
    # singleton lookup is filter_by(title=...).first() with no ORDER BY, so which
    # row wins is arbitrary — the same alert hijack the create guard blocks.
    if _is_internal(data.get("title")):
        logger.warning(
            "Blocked rename of item %s into the reserved namespace (%r) by user %s",
            item.id, data.get("title"), user_id,
        )
        return jsonify({"error": "Titles beginning with '__' are reserved for system records"}), 403

    # NOTE: ordinary signal/watchlist rows stay team-writable on purpose. Every
    # row is visible to the whole team (see list_items), watchlist entries are
    # owned by whichever account ran the scan that auto-added them, and the UI
    # lets any teammate mute/annotate them — an owner-only rule here would 403
    # every non-admin doing normal work. The real exposure was never shared
    # editing of signals; it is the internal control rows, which _is_internal_item
    # hides from everyone including admins.

    for field, value in data.items():
        setattr(item, field, value)
    db.session.commit()
    return jsonify({"item": item.to_dict()}), 200


@bp.route("/<int:item_id>", methods=["DELETE"])
@jwt_required()
def delete_item(item_id):
    """Delete an item (owner or admin).
    ---
    tags: [Items]
    security:
      - Bearer: []
    parameters:
      - in: path
        name: item_id
        required: true
        schema: {type: integer}
    responses:
      200:
        description: Deleted
    """
    user_id = int(get_jwt_identity())
    user = db.session.get(User, user_id)
    item = db.session.get(Item, item_id)

    if not item or _is_internal_item(item):
        if item is not None:
            logger.warning(
                "Blocked delete of internal row %s (%r) by user %s",
                item.id, item.title, user_id,
            )
        return jsonify({"error": "Item not found"}), 404
    if item.owner_id != user_id and not (user and user.is_admin()):
        return jsonify({"error": "Forbidden"}), 403

    db.session.delete(item)
    db.session.commit()
    return jsonify({"message": "Item deleted"}), 200


@bp.route("/bulk", methods=["POST"])
@jwt_required()
def bulk_create_items():
    """Bulk-create items in a single transaction (max 5000).

    Used for LinkedIn CSV import and other seed-list imports.
    Accepts {"items": [{"title": str, "description": str}, ...]}
    Returns {"created": N}
    """
    import json as _json
    data = request.get_json() or {}
    raw_items = data.get("items")

    if not raw_items or not isinstance(raw_items, list):
        return jsonify({"error": "items must be a non-empty list"}), 422
    if len(raw_items) > 5000:
        return jsonify({"error": "max 5000 items per request"}), 422

    user_id = int(get_jwt_identity())
    created_count = 0

    for entry in raw_items:
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        if _is_internal(title):
            logger.warning(
                "Skipped reserved-title item %r in bulk import by user %s", title, user_id
            )
            continue
        raw_desc = entry.get("description") or ""
        try:
            _parsed_type = _json.loads(raw_desc).get("_type") if raw_desc else None
        except Exception:
            _parsed_type = None
        item = Item(title=title, description=raw_desc, item_type=_parsed_type, owner_id=user_id)
        db.session.add(item)
        created_count += 1

    db.session.commit()
    return jsonify({"created": created_count}), 201
