from flask import request, jsonify, current_app
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    jwt_required,
    get_jwt_identity,
    get_jwt,
    decode_token,
)
from itsdangerous import SignatureExpired, BadSignature
from marshmallow import ValidationError
from . import bp
from ...extensions import db, bcrypt
from ...models.user import User
from ...models.token_blocklist import TokenBlocklist
from ...schemas import LogoutSchema, ForgotPasswordSchema, ResetPasswordSchema
from ...services.tokens import generate_reset_token, verify_reset_token
from ...services.email import send_password_reset_email

logout_schema = LogoutSchema()
forgot_password_schema = ForgotPasswordSchema()
reset_password_schema = ResetPasswordSchema()


@bp.route("/register", methods=["POST"])
def register():
    data = request.get_json()

    required = ("email", "password", "first_name", "last_name")
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    if User.query.filter_by(email=data["email"].lower()).first():
        return jsonify({"error": "Email already registered"}), 409

    user = User(
        email=data["email"].lower(),
        first_name=data["first_name"],
        last_name=data["last_name"],
    )
    user.set_password(data["password"])

    db.session.add(user)
    db.session.commit()

    access_token = create_access_token(identity=str(user.id))
    refresh_token = create_refresh_token(identity=str(user.id))

    return jsonify({
        "user": user.to_dict(),
        "access_token": access_token,
        "refresh_token": refresh_token,
    }), 201


@bp.route("/login", methods=["POST"])
def login():
    data = request.get_json()

    if not data.get("email") or not data.get("password"):
        return jsonify({"error": "Email and password required"}), 400

    user = User.query.filter_by(email=data["email"].lower()).first()

    if not user or not user.check_password(data["password"]):
        return jsonify({"error": "Invalid email or password"}), 401

    if not user.is_active:
        return jsonify({"error": "Account is inactive"}), 403

    access_token = create_access_token(identity=str(user.id))
    refresh_token = create_refresh_token(identity=str(user.id))

    return jsonify({
        "user": user.to_dict(),
        "access_token": access_token,
        "refresh_token": refresh_token,
    }), 200


@bp.route("/logout", methods=["POST"])
@jwt_required()
def logout():
    # Always revoke the access token
    db.session.add(TokenBlocklist(jti=get_jwt()["jti"]))

    # Optionally revoke the refresh token if the client sends it
    data = request.get_json(silent=True) or {}
    refresh_token_str = data.get("refresh_token")
    if refresh_token_str:
        try:
            decoded = decode_token(refresh_token_str)
            refresh_jti = decoded["jti"]
            if not TokenBlocklist.query.filter_by(jti=refresh_jti).first():
                db.session.add(TokenBlocklist(jti=refresh_jti))
        except Exception:
            pass  # invalid/expired refresh token — access token still revoked

    db.session.commit()
    return jsonify({"message": "Successfully logged out"}), 200


@bp.route("/me", methods=["GET"])
@jwt_required()
def me():
    user_id = int(get_jwt_identity())
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify({"user": user.to_dict()}), 200


@bp.route("/refresh", methods=["POST"])
@jwt_required(refresh=True)
def refresh():
    user_id = get_jwt_identity()
    access_token = create_access_token(identity=str(user_id))
    return jsonify({"access_token": access_token}), 200


@bp.route("/forgot-password", methods=["POST"])
def forgot_password():
    try:
        data = forgot_password_schema.load(request.get_json() or {})
    except ValidationError as e:
        return jsonify({"error": e.messages}), 422

    user = User.query.filter_by(email=data["email"].lower()).first()
    if user:
        token = generate_reset_token(current_app.config["SECRET_KEY"], user.id)
        frontend_url = current_app.config.get("FRONTEND_URL", "http://localhost:5173")
        reset_url = f"{frontend_url}/reset-password?token={token}"
        try:
            send_password_reset_email(user.email, reset_url)
        except Exception as e:
            current_app.logger.error("Failed to send reset email: %s", str(e))
            return jsonify({"error": "Email service unavailable"}), 500

    # Always 200 — prevents email enumeration
    return jsonify({"message": "If that email exists, a reset link has been sent"}), 200


@bp.route("/reset-password", methods=["POST"])
def reset_password():
    try:
        data = reset_password_schema.load(request.get_json() or {})
    except ValidationError as e:
        return jsonify({"error": e.messages}), 422

    try:
        user_id = verify_reset_token(current_app.config["SECRET_KEY"], data["token"])
    except SignatureExpired:
        return jsonify({"error": "Reset token has expired"}), 400
    except BadSignature:
        return jsonify({"error": "Invalid reset token"}), 400

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    user.set_password(data["password"])
    db.session.commit()
    return jsonify({"message": "Password reset successfully"}), 200
