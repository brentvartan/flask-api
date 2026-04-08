import json
import logging
import os
import time as _time
from calendar import monthrange
from datetime import datetime, timezone

import requests
from flask import request, jsonify, current_app
from flask_jwt_extended import get_jwt_identity
from marshmallow import ValidationError
from sqlalchemy import func

from . import bp
from ...extensions import db
from ...models.user import User
from ...models.item import Item
from ...schemas import AdminUserUpdateSchema, AdminForcePasswordSchema, PaginationSchema
from ...services.tokens import generate_reset_token
from ...services.email import send_password_reset_email
from ...utils import admin_required

logger = logging.getLogger(__name__)

pagination_schema      = PaginationSchema()
admin_user_update_schema = AdminUserUpdateSchema()
admin_force_pwd_schema   = AdminForcePasswordSchema()


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
    """Update a user's name, role, or active status (admin only).
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
            properties:
              first_name: {type: string}
              last_name:  {type: string}
              role:       {type: string, enum: [user, admin]}
              is_active:  {type: boolean}
    responses:
      200:
        description: Updated user
    """
    current_user_id = int(get_jwt_identity())

    try:
        data = admin_user_update_schema.load(request.get_json() or {})
    except ValidationError as e:
        return jsonify({"error": e.messages}), 422

    if not data:
        return jsonify({"error": "No fields provided"}), 400

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    # Guard: admins cannot change their own role or deactivate themselves
    if user_id == current_user_id:
        if "role" in data:
            return jsonify({"error": "You cannot change your own role"}), 403
        if "is_active" in data and not data["is_active"]:
            return jsonify({"error": "You cannot deactivate your own account"}), 403

    for field in ("first_name", "last_name", "role", "is_active"):
        if field in data:
            setattr(user, field, data[field])

    db.session.commit()
    return jsonify({"user": user.to_dict()}), 200


@bp.route("/users/<int:user_id>/force-reset", methods=["POST"])
@admin_required()
def force_reset_password(user_id):
    """Force-set a user's password (admin only). Password is stored hashed — never returned.
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
            required: [password]
            properties:
              password: {type: string, minLength: 8}
    responses:
      200:
        description: Password updated
    """
    try:
        data = admin_force_pwd_schema.load(request.get_json() or {})
    except ValidationError as e:
        return jsonify({"error": e.messages}), 422

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    user.set_password(data["password"])
    db.session.commit()
    return jsonify({"message": f"Password updated for {user.email}"}), 200


# ─── Spend / usage dashboard ──────────────────────────────────────────────────

_spend_cache: dict = {"data": None, "expires": 0}

# Cost constants (update if pricing changes)
_PROXYCURL_COST_PER_LOOKUP   = 0.01    # ~1 credit per profile fetch @ $0.01/credit
_SERPAPI_COST_PER_SEARCH     = 0.00    # free plan (250/mo); paid plan ~$0.01/search
_ANTHROPIC_COST_PER_SIGNAL   = 0.03    # claude-sonnet avg per enrichment call
_ANTHROPIC_HAIKU_PER_RESCORE = 0.005   # claude-haiku founder re-score call
_CRUNCHBASE_COST_PER_LOOKUP  = 0.0    # TBD — depends on plan tier; update when known
_RESEND_COST_PER_EMAIL       = 0.0    # Free tier: 3,000/mo; update if upgraded


def _proxycurl_credits() -> dict:
    """Fetch live credit balance from Proxycurl (NinjaPear) API."""
    api_key = os.environ.get("PROXYCURL_API_KEY")
    if not api_key:
        return {"available": None, "error": "PROXYCURL_API_KEY not set"}
    try:
        resp = requests.get(
            "https://nubela.co/proxycurl/api/credit-balance",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return {"available": data.get("credit_balance"), "error": None}
        return {"available": None, "error": f"HTTP {resp.status_code}"}
    except Exception as exc:
        logger.warning("Proxycurl credit check failed: %s", repr(exc))
        return {"available": None, "error": "Unable to reach Proxycurl API"}


def _serpapi_stats() -> dict:
    """Fetch SerpAPI account stats (searches remaining, plan)."""
    api_key = os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        return {"searches_left": None, "searches_per_month": None, "this_month_usage": None, "error": "SERPAPI_API_KEY not set"}
    try:
        resp = requests.get(
            "https://serpapi.com/account",
            params={"api_key": api_key},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "searches_left":      data.get("plan_searches_left"),
                "searches_per_month": data.get("searches_per_month"),
                "this_month_usage":   data.get("this_month_usage"),
                "plan_name":          data.get("plan_name", "Free"),
                "error": None,
            }
        return {"searches_left": None, "searches_per_month": None, "this_month_usage": None, "error": f"HTTP {resp.status_code}"}
    except Exception as exc:
        logger.warning("SerpAPI stats check failed: %s", repr(exc))
        return {"searches_left": None, "searches_per_month": None, "this_month_usage": None, "error": "Unable to reach SerpAPI"}


def _count_signals_this_month(field_check: str) -> int:
    """Count signals created this month matching a JSON field check."""
    now   = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (
        Item.query
        .filter(
            Item.item_type == 'signal',
            Item.description.contains(field_check),
            Item.created_at >= start,
        )
        .count()
    )


def _count_signals_all_time(field_check: str) -> int:
    return (
        Item.query
        .filter(
            Item.item_type == 'signal',
            Item.description.contains(field_check),
        )
        .count()
    )


def _resend_email_stats():
    """Count HOT alert emails sent this month and all-time from scan_runs."""
    try:
        from ...models.scan_run import ScanRun
        from datetime import datetime, timezone, timedelta
        month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        this_month = ScanRun.query.filter(
            ScanRun.alert_sent == True,
            ScanRun.ran_at >= month_start,
        ).count()
        all_time = ScanRun.query.filter(ScanRun.alert_sent == True).count()
        return {"emails_this_month": this_month, "emails_all_time": all_time, "error": None}
    except Exception as e:
        logger.warning("Resend email stats query failed: %s", repr(e))
        return {"emails_this_month": 0, "emails_all_time": 0, "error": "Stats unavailable"}


@bp.route("/spend", methods=["GET"])
@admin_required()
def get_spend():
    """Admin-only spend and usage dashboard."""

    now = _time.time()
    if _spend_cache["data"] and _spend_cache["expires"] > now:
        return jsonify(_spend_cache["data"]), 200

    # Live balances from external APIs
    proxycurl = _proxycurl_credits()
    serpapi   = _serpapi_stats()

    # LinkedIn (Proxycurl) enrichment counts
    linkedin_month   = _count_signals_this_month('"linkedin_enriched":true')
    linkedin_alltime = _count_signals_all_time('"linkedin_enriched":true')

    # SerpAPI search counts (founder discovery — stored as founder_discovered)
    serpapi_month   = _count_signals_this_month('"founder_discovered":true')
    serpapi_alltime = _count_signals_all_time('"founder_discovered":true')

    # Anthropic enrichment counts
    anthropic_month   = _count_signals_this_month('"enriched":true')
    anthropic_alltime = _count_signals_all_time('"enriched":true')

    # Crunchbase enrichment counts
    crunchbase_month   = _count_signals_this_month('"crunchbase_enriched":true')
    crunchbase_alltime = _count_signals_all_time('"crunchbase_enriched":true')

    # Resend email counts
    resend = _resend_email_stats()

    # Cost estimates
    proxycurl_cost_month   = round(linkedin_month   * _PROXYCURL_COST_PER_LOOKUP, 2)
    proxycurl_cost_alltime = round(linkedin_alltime * _PROXYCURL_COST_PER_LOOKUP, 2)

    serpapi_cost_month   = round(serpapi_month   * _SERPAPI_COST_PER_SEARCH, 2)
    serpapi_cost_alltime = round(serpapi_alltime * _SERPAPI_COST_PER_SEARCH, 2)

    anthropic_cost_month   = round(
        (anthropic_month * _ANTHROPIC_COST_PER_SIGNAL) +
        (linkedin_month  * _ANTHROPIC_HAIKU_PER_RESCORE), 2
    )
    anthropic_cost_alltime = round(
        (anthropic_alltime * _ANTHROPIC_COST_PER_SIGNAL) +
        (linkedin_alltime  * _ANTHROPIC_HAIKU_PER_RESCORE), 2
    )

    crunchbase_cost_month   = round(crunchbase_month   * _CRUNCHBASE_COST_PER_LOOKUP, 2)
    crunchbase_cost_alltime = round(crunchbase_alltime * _CRUNCHBASE_COST_PER_LOOKUP, 2)

    resend_cost_month   = round(resend["emails_this_month"] * _RESEND_COST_PER_EMAIL, 2)
    resend_cost_alltime = round(resend["emails_all_time"]   * _RESEND_COST_PER_EMAIL, 2)

    total_cost_month   = round(proxycurl_cost_month   + serpapi_cost_month   + anthropic_cost_month   + crunchbase_cost_month   + resend_cost_month,   2)
    total_cost_alltime = round(proxycurl_cost_alltime + serpapi_cost_alltime + anthropic_cost_alltime + crunchbase_cost_alltime + resend_cost_alltime, 2)

    result_dict = {
        "proxycurl": {
            "credits_available":         proxycurl["available"],
            "error":                     proxycurl["error"],
            "lookups_this_month":        linkedin_month,
            "lookups_all_time":          linkedin_alltime,
            "estimated_cost_this_month": proxycurl_cost_month,
            "estimated_cost_all_time":   proxycurl_cost_alltime,
            "cost_per_lookup":           _PROXYCURL_COST_PER_LOOKUP,
        },
        "serpapi": {
            "searches_left":             serpapi["searches_left"],
            "searches_per_month":        serpapi["searches_per_month"],
            "this_month_usage":          serpapi["this_month_usage"],
            "plan_name":                 serpapi.get("plan_name", "Free"),
            "searches_this_month":       serpapi_month,
            "searches_all_time":         serpapi_alltime,
            "estimated_cost_this_month": serpapi_cost_month,
            "estimated_cost_all_time":   serpapi_cost_alltime,
            "error":                     serpapi["error"],
        },
        "anthropic": {
            "enrichments_this_month":    anthropic_month,
            "enrichments_all_time":      anthropic_alltime,
            "estimated_cost_this_month": anthropic_cost_month,
            "estimated_cost_all_time":   anthropic_cost_alltime,
            "cost_per_enrichment":       _ANTHROPIC_COST_PER_SIGNAL,
        },
        "crunchbase": {
            "lookups_this_month":        crunchbase_month,
            "lookups_all_time":          crunchbase_alltime,
            "estimated_cost_this_month": crunchbase_cost_month,
            "estimated_cost_all_time":   crunchbase_cost_alltime,
            "cost_per_lookup":           _CRUNCHBASE_COST_PER_LOOKUP,
            "active":                    bool(os.environ.get("CRUNCHBASE_API_KEY")),
        },
        "resend": {
            "emails_this_month":         resend["emails_this_month"],
            "emails_all_time":           resend["emails_all_time"],
            "estimated_cost_this_month": resend_cost_month,
            "estimated_cost_all_time":   resend_cost_alltime,
            "error":                     resend["error"],
            "plan":                      "Free (3,000/mo)",
        },
        "totals": {
            "estimated_cost_this_month": total_cost_month,
            "estimated_cost_all_time":   total_cost_alltime,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _spend_cache["data"] = result_dict
    _spend_cache["expires"] = _time.time() + 300  # 5 minutes
    return jsonify(result_dict), 200


@bp.route("/users/<int:user_id>", methods=["DELETE"])
@admin_required()
def delete_user(user_id):
    """Permanently delete a user account (admin only). Cannot delete yourself.
    ---
    tags: [Admin]
    security:
      - Bearer: []
    parameters:
      - in: path
        name: user_id
        required: true
        schema: {type: integer}
    responses:
      200:
        description: User deleted
      403:
        description: Cannot delete yourself
      404:
        description: User not found
    """
    current_user_id = int(get_jwt_identity())

    if user_id == current_user_id:
        return jsonify({"error": "You cannot delete your own account"}), 403

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    db.session.delete(user)
    db.session.commit()
    return jsonify({"message": f"User {user.email} deleted"}), 200


@bp.route("/users/<int:user_id>/send-reset", methods=["POST"])
@admin_required()
def send_reset_link(user_id):
    """Send a password-reset link email to a user (admin only)."""
    # Guard: can't reset your own password via admin panel
    current_user_id = int(get_jwt_identity())
    if user_id == current_user_id:
        return jsonify({"error": "Use the forgot-password flow to reset your own password"}), 403

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    token = generate_reset_token(current_app.config["SECRET_KEY"], user.id)
    frontend_url = current_app.config.get("FRONTEND_URL", "https://brentvartan.github.io/stealth-finder-frontend")
    reset_url = f"{frontend_url}/reset-password?token={token}"

    try:
        send_password_reset_email(user.email, reset_url)
    except Exception as e:
        current_app.logger.error("Failed to send reset email for user %d: %s", user_id, str(e))
        return jsonify({"error": "Email service unavailable"}), 500

    return jsonify({"message": f"Reset link sent to {user.email}"}), 200
