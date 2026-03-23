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
from ...schemas import LogoutSchema, ForgotPasswordSchema, ResetPasswordSchema, InviteSchema, AcceptInviteSchema
from ...services.tokens import generate_reset_token, verify_reset_token, generate_invite_token, verify_invite_token
from ...services.email import send_password_reset_email, send_invite_email
from ...utils import db_get_user

logout_schema = LogoutSchema()
forgot_password_schema = ForgotPasswordSchema()
reset_password_schema = ResetPasswordSchema()
invite_schema = InviteSchema()
accept_invite_schema = AcceptInviteSchema()


@bp.route("/register", methods=["POST"])
def register():
    data = request.get_json()

    required = ("email", "password", "first_name", "last_name")
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    if not data["email"].lower().endswith("@bullish.co"):
        return jsonify({"error": "Access is restricted to @bullish.co email addresses"}), 403

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


@bp.route("/invite", methods=["POST"])
@jwt_required()
def invite():
    """Send a team invite email (admin only)."""
    current_user = db_get_user()
    if not current_user or not current_user.is_admin():
        return jsonify({"error": "Admin access required"}), 403

    try:
        data = invite_schema.load(request.get_json() or {})
    except ValidationError as e:
        return jsonify({"error": e.messages}), 422

    email = data["email"].lower()

    if User.query.filter_by(email=email).first():
        return jsonify({"error": "An account with that email already exists"}), 409

    token = generate_invite_token(current_app.config["SECRET_KEY"], email)
    frontend_url = current_app.config.get("FRONTEND_URL", "http://localhost:5173")
    invite_url = f"{frontend_url}/accept-invite?token={token}"
    invited_by = f"{current_user.first_name} {current_user.last_name}"

    try:
        send_invite_email(email, invite_url, invited_by)
    except Exception as e:
        current_app.logger.error("Failed to send invite email: %s", str(e))
        return jsonify({"error": "Email service unavailable"}), 500

    return jsonify({"message": f"Invite sent to {email}"}), 200


@bp.route("/accept-invite", methods=["POST"])
def accept_invite():
    """Accept a team invite — verify token, create account, return JWT."""
    try:
        data = accept_invite_schema.load(request.get_json() or {})
    except ValidationError as e:
        return jsonify({"error": e.messages}), 422

    try:
        email = verify_invite_token(current_app.config["SECRET_KEY"], data["token"])
    except SignatureExpired:
        return jsonify({"error": "Invite link has expired — ask your admin to resend"}), 400
    except BadSignature:
        return jsonify({"error": "Invalid invite link"}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({"error": "An account with that email already exists. Try logging in."}), 409

    user = User(
        email=email,
        first_name=data["first_name"],
        last_name=data["last_name"],
        role="user",
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
