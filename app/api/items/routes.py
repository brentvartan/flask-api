from flask import request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from marshmallow import ValidationError
from . import bp
from ...extensions import db
from ...models.item import Item
from ...models.user import User
from ...schemas import ItemSchema, ItemUpdateSchema, PaginationSchema

item_schema = ItemSchema()
item_update_schema = ItemUpdateSchema()
pagination_schema = PaginationSchema()


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

    user_id = int(get_jwt_identity())
    page = params["page"]
    per_page = params["per_page"]

    pagination = (
        Item.query
        .filter_by(owner_id=user_id)
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
    item = Item(title=data["title"], description=data.get("description"), owner_id=user_id)
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
    user_id = int(get_jwt_identity())
    item = db.session.get(Item, item_id)
    if not item:
        return jsonify({"error": "Item not found"}), 404
    if item.owner_id != user_id:
        return jsonify({"error": "Forbidden"}), 403
    return jsonify({"item": item.to_dict()}), 200


@bp.route("/<int:item_id>", methods=["PUT"])
@jwt_required()
def update_item(item_id):
    """Update an item (owner only).
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
    item = db.session.get(Item, item_id)
    if not item:
        return jsonify({"error": "Item not found"}), 404
    if item.owner_id != user_id:
        return jsonify({"error": "Forbidden"}), 403

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
    if item.owner_id != user_id and not user.is_admin():
        return jsonify({"error": "Forbidden"}), 403

    db.session.delete(item)
    db.session.commit()
    return jsonify({"message": "Item deleted"}), 200
