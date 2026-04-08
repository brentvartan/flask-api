from functools import wraps
from flask import jsonify
from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
from ..extensions import db
from ..models.user import User


def db_get_user():
    user_id = int(get_jwt_identity())
    return db.session.get(User, user_id)


def admin_required():
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            verify_jwt_in_request()
            user = db_get_user()
            if not user or not user.is_admin():
                return jsonify({"error": "Admin access required"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator
