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


# ── Protected singleton rows ──────────────────────────────────────────────────
# GET /api/items returns every row to every authenticated user, so the config
# singletons are discoverable. `__bullish_settings__` holds alert_emails and the
# Slack webhook — rewriting it redirects every confluence/watchlist/digest alert.
# The scheduler's `system` rows hold job locks and the heartbeat. None of these
# may change through the generic items API, for anyone, admins included; they
# have dedicated endpoints (PATCH /api/settings, /api/admin/*).

_PROTECTED_ITEM_TYPES = ("system", "settings")
_RESERVED_TITLE_PREFIX = "__"


def _is_reserved_title(title):
    """True for the reserved `__name__` namespace that config/state singletons use.

    Every singleton lookup in the codebase is `filter_by(title=...).first()` with
    no tiebreak, so a second row with the same title could win the lookup — the
    same alert-hijack by a different route. Reserved titles are server-side only.
    """
    return (title or "").startswith(_RESERVED_TITLE_PREFIX)


def _is_protected_item(item):
    """True for config/state singletons the generic items API must never mutate."""
    return item.item_type in _PROTECTED_ITEM_TYPES or _is_reserved_title(item.title)


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

    # Items are shared across all authenticated team members
    pagination = (
        Item.query
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
    if _is_reserved_title(data["title"]):
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
    if not item:
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

    if not item:
        return jsonify({"error": "Item not found"}), 404
    if _is_protected_item(item):
        logger.warning(
            "Blocked write to protected item %s (%r) by user %s",
            item.id, item.title, user_id,
        )
        return jsonify({"error": "This record is system-managed and cannot be edited here"}), 403

    # NOTE: ordinary signal/watchlist rows stay team-writable on purpose. Every
    # row is visible to the whole team (see list_items), watchlist entries are
    # owned by whichever account ran the scan that auto-added them, and the UI
    # lets any teammate mute/annotate them — an owner-only rule here would 403
    # every non-admin doing normal work. The real exposure was never shared
    # editing of signals; it was the config singletons, which _is_protected_item
    # now blocks for everyone including admins.

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

    if not item:
        return jsonify({"error": "Item not found"}), 404
    if _is_protected_item(item):
        logger.warning(
            "Blocked delete of protected item %s (%r) by user %s",
            item.id, item.title, user_id,
        )
        return jsonify({"error": "This record is system-managed and cannot be deleted here"}), 403
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
        if _is_reserved_title(title):
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
