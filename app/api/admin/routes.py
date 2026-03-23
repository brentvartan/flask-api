import json
import os
from calendar import monthrange
from datetime import datetime, timezone

import requests
from flask import request, jsonify
from marshmallow import ValidationError
from sqlalchemy import func

from . import bp
from ...extensions import db
from ...models.user import User
from ...models.item import Item
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


# ─── Spend / usage dashboard ──────────────────────────────────────────────────

# Cost constants (update if pricing changes)
_ENRICH_LAYER_COST_PER_LOOKUP = 0.04   # search + profile = ~4 credits @ $0.01/credit
_ANTHROPIC_COST_PER_SIGNAL    = 0.03   # claude-sonnet avg per enrichment call
_ANTHROPIC_HAIKU_PER_RESCORE  = 0.005  # claude-haiku founder re-score call


def _enrich_layer_credits() -> dict:
    """Fetch live credit balance from Enrich Layer API."""
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
        return {"available": None, "error": str(exc)}


def _count_signals_this_month(field_check: str) -> int:
    """Count signals created this month matching a JSON field check."""
    now   = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (
        Item.query
        .filter(
            Item.description.contains('"_type":"signal"'),
            Item.description.contains(field_check),
            Item.created_at >= start,
        )
        .count()
    )


def _count_signals_all_time(field_check: str) -> int:
    return (
        Item.query
        .filter(
            Item.description.contains('"_type":"signal"'),
            Item.description.contains(field_check),
        )
        .count()
    )


@bp.route("/spend", methods=["GET"])
@admin_required()
def get_spend():
    """Admin-only spend and usage dashboard."""

    # Live credit balance from Enrich Layer
    enrich_layer = _enrich_layer_credits()

    # LinkedIn enrichment counts
    linkedin_month    = _count_signals_this_month('"linkedin_enriched":true')
    linkedin_alltime  = _count_signals_all_time('"linkedin_enriched":true')

    # Anthropic enrichment counts
    anthropic_month   = _count_signals_this_month('"enriched":true')
    anthropic_alltime = _count_signals_all_time('"enriched":true')

    # Cost estimates
    linkedin_cost_month    = round(linkedin_month   * _ENRICH_LAYER_COST_PER_LOOKUP, 2)
    linkedin_cost_alltime  = round(linkedin_alltime * _ENRICH_LAYER_COST_PER_LOOKUP, 2)
    anthropic_cost_month   = round(
        (anthropic_month * _ANTHROPIC_COST_PER_SIGNAL) +
        (linkedin_month  * _ANTHROPIC_HAIKU_PER_RESCORE), 2
    )
    anthropic_cost_alltime = round(
        (anthropic_alltime * _ANTHROPIC_COST_PER_SIGNAL) +
        (linkedin_alltime  * _ANTHROPIC_HAIKU_PER_RESCORE), 2
    )

    total_cost_month   = round(linkedin_cost_month   + anthropic_cost_month,   2)
    total_cost_alltime = round(linkedin_cost_alltime + anthropic_cost_alltime, 2)

    return jsonify({
        "enrich_layer": {
            "credits_available": enrich_layer["available"],
            "error":             enrich_layer["error"],
            "lookups_this_month": linkedin_month,
            "lookups_all_time":   linkedin_alltime,
            "estimated_cost_this_month": linkedin_cost_month,
            "estimated_cost_all_time":   linkedin_cost_alltime,
            "cost_per_lookup":           _ENRICH_LAYER_COST_PER_LOOKUP,
        },
        "anthropic": {
            "enrichments_this_month": anthropic_month,
            "enrichments_all_time":   anthropic_alltime,
            "estimated_cost_this_month": anthropic_cost_month,
            "estimated_cost_all_time":   anthropic_cost_alltime,
            "cost_per_enrichment":       _ANTHROPIC_COST_PER_SIGNAL,
        },
        "totals": {
            "estimated_cost_this_month": total_cost_month,
            "estimated_cost_all_time":   total_cost_alltime,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }), 200
