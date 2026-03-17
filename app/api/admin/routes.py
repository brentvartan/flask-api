from flask import request, jsonify
from marshmallow import ValidationError
from . import bp
from ...extensions import db
from ...models.user import User
from ...schemas import UserUpdateSchema, PaginationSchema
from ...utils import admin_required

pagination_schema = PaginationSchema()
user_update_schema = UserUpdateSchema()


@bp.route("/users", methods=["GET"])
@admin_required()
def list_users():
    """List all users (admin only).
    ---
    tags: [Admin]
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
        description: Paginated list of users
    """
    try:
        params = pagination_schema.load(request.args)
    except ValidationError as e:
        return jsonify({"error": e.messages}), 422

    pagination = (
        User.query
        .order_by(User.created_at.desc())
        .paginate(page=params["page"], per_page=params["per_page"], error_out=False)
    )

    return jsonify({
        "users": [u.to_dict() for u in pagination.items],
        "pagination": {
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
            "pages": pagination.pages,
        },
    }), 200


@bp.route("/users/<int:user_id>", methods=["PATCH"])
@admin_required()
def update_user(user_id):
    """Activate or deactivate a user (admin only).
    ---
    tags: [Admin]
    security:
      - Bearer: []
    parameters:
      - in: path
        name: user_id
        required: true
        schema: {type: integer}
    requestBody:
      required: true
      content:
        application/json:
          schema:
            required: [is_active]
            properties:
              is_active: {type: boolean}
    responses:
      200:
        description: Updated user
    """
    try:
        data = user_update_schema.load(request.get_json() or {})
    except ValidationError as e:
        return jsonify({"error": e.messages}), 422

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    user.is_active = data["is_active"]
    db.session.commit()
    return jsonify({"user": user.to_dict()}), 200
