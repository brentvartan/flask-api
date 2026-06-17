import json
import logging
import os
import time as _time
from calendar import monthrange
from collections import Counter
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
from ...models.founder_profile import FounderProfile
from ...schemas import AdminUserUpdateSchema, AdminForcePasswordSchema, PaginationSchema
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
            "https://nubela.co/api/v1/meta/credit-balance",
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


@bp.route("/run-press-monitor", methods=["POST"])
@admin_required()
def run_press_monitor():
    """Manually trigger the trade press cross-reference scan for all brands in DB."""
    import threading
    from ...services.scheduler import _run_press_monitor as _press_job
    app = current_app._get_current_object()
    threading.Thread(target=_press_job, args=(app,), daemon=True).start()
    return jsonify({"message": "Press monitor started — check back in ~2 minutes"}), 202


@bp.route("/check-all-domains", methods=["POST"])
@admin_required()
def check_all_domains():
    """Queue domain status checks for all domain signals that haven't been checked yet."""
    import threading
    from ...services.scheduler import _check_domain_bg

    app = current_app._get_current_object()

    domain_items = (
        Item.query
        .filter(
            Item.item_type == 'signal',
            Item.description.contains('"signal_type":"domain"'),
        )
        .limit(300)
        .all()
    )

    queued = 0
    for item in domain_items:
        try:
            meta = json.loads(item.description or '{}')
            if meta.get('signal_type') != 'domain':
                continue
            if meta.get('domain_status'):
                continue  # already checked
            url = meta.get('url')
            if not url:
                continue
            threading.Thread(
                target=_check_domain_bg,
                args=(app, item.id, url),
                daemon=True,
            ).start()
            queued += 1
        except Exception as exc:
            logger.warning("Domain check queue failed for item %s: %s", item.id, exc)

    return jsonify({
        "message": f"Domain check queued for {queued} signal{'s' if queued != 1 else ''} — results appear within ~1 minute",
        "queued": queued,
    }), 202


@bp.route("/inbox-audit/run", methods=["POST"])
@admin_required()
def run_inbox_audit():
    """
    Check a list of deal brand names against the Stealth Finder signal DB.

    Accepts JSON: {"brand_names": ["Filament", "Stripes Beauty", ...]}

    Called monthly by the Claude Code scheduled task that reads the
    INVESTMENTS/A. Deals Gmail label and extracts brand names via Claude.
    Can also be triggered manually from the Settings → Tools page.
    """
    from ...services.inbox_audit import check_brands_in_db
    body = request.get_json(silent=True) or {}
    brand_names = body.get("brand_names", [])
    if not isinstance(brand_names, list) or not brand_names:
        return jsonify({"error": "brand_names must be a non-empty list"}), 400
    if len(brand_names) > 500:
        return jsonify({"error": "brand_names list too large (max 500)"}), 400

    app = current_app._get_current_object()
    result = check_brands_in_db(brand_names, app)
    return jsonify(result), 200


@bp.route("/inbox-audit/latest", methods=["GET"])
@admin_required()
def get_inbox_audit():
    """Return the most recent inbox audit result."""
    from ...services.inbox_audit import get_latest_audit
    app = current_app._get_current_object()
    result = get_latest_audit(app)
    if not result:
        return jsonify({"error": "No audit has been run yet"}), 404
    return jsonify(result), 200


@bp.route("/watchlist/bulk-import", methods=["POST"])
@admin_required()
def bulk_import_watchlist():
    """
    POST /api/admin/watchlist/bulk-import

    Bulk-add brands to the watchlist. Skips brands already present.
    Accepts JSON: {"brands": [{"name": "Poppi", "category": "Beverage", "notes": "...", "deal_type": "Acquisition", "acquirer": "PepsiCo", "year": 2025}, ...]}
    Returns: {"added": [...], "skipped": [...], "total": N}
    """
    body = request.get_json(silent=True) or {}
    brands = body.get("brands", [])
    if not isinstance(brands, list) or not brands:
        return jsonify({"error": "brands must be a non-empty list"}), 400
    if len(brands) > 200:
        return jsonify({"error": "brands list too large (max 200)"}), 400

    owner_id = int(get_jwt_identity())
    existing = Item.query.filter(
        Item.owner_id == owner_id,
        Item.item_type == "watchlist",
    ).all()
    existing_keys = set()
    for item in existing:
        try:
            meta = json.loads(item.description or "{}")
            key = (meta.get("company") or item.title or "").upper().strip()
            existing_keys.add(key)
        except Exception:
            existing_keys.add((item.title or "").upper().strip())

    added, skipped = [], []
    now = datetime.now(timezone.utc).isoformat()

    for b in brands:
        brand_name = (b.get("name") or "").strip()
        if not brand_name:
            continue
        brand_key = brand_name.upper().strip()
        if brand_key in existing_keys:
            skipped.append(brand_name)
            continue

        deal_parts = []
        if b.get("deal_type"):
            deal_parts.append(b["deal_type"])
        if b.get("acquirer") and b["acquirer"] not in ("-", "Public Markets"):
            deal_parts.append(f"→ {b['acquirer']}")
        if b.get("year"):
            deal_parts.append(str(int(b["year"])))
        deal_str = " ".join(deal_parts)

        notes_parts = []
        if b.get("what_they_do"):
            notes_parts.append(b["what_they_do"])
        if deal_str:
            notes_parts.append(deal_str)
        if b.get("notes"):
            notes_parts.append(b["notes"])

        meta = {
            "_type":          "watchlist",
            "name":           "",
            "company":        brand_name,
            "linkedin":       "",
            "twitter":        "",
            "notes":          " · ".join(notes_parts) if notes_parts else f"Imported from Consumer Deals list",
            "added_at":       now,
            "auto_added":     True,
            "bulk_imported":  True,
            "category":       b.get("category", ""),
            "deal_type":      b.get("deal_type", ""),
            "acquirer":       b.get("acquirer", ""),
            "deal_year":      int(b["year"]) if b.get("year") and str(b.get("year")).isdigit() else None,
            "valuation_m":    b.get("valuation_m"),
            "signal_types":   [],
            "rescore_history": [],
            "last_news_check": None,
            "news_results":   [],
        }

        db.session.add(Item(
            title=brand_name,
            owner_id=owner_id,
            item_type="watchlist",
            description=json.dumps(meta),
        ))
        existing_keys.add(brand_key)
        added.append(brand_name)

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error("bulk_import_watchlist failed: %s", exc)
        return jsonify({"error": "Database error during bulk import"}), 500

    return jsonify({
        "added":   added,
        "skipped": skipped,
        "total":   len(added) + len(skipped),
    }), 200


@bp.route("/users/<int:user_id>/send-reset", methods=["POST"])
@admin_required()
def send_reset_link(user_id):
    """Send a password-reset link email to a user (admin only)."""
    from ...services.tokens import generate_reset_token
    from ...services.email import send_password_reset_email
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


# ─── Founder Radar ────────────────────────────────────────────────────────────

_TIER_ORDER = {"DEPARTURE": 0, "CONVICTION": 1, "ALUMNI": 2, "EXEC": 3}
_STATUS_ORDER = {"building": 0, "advisory": 1, "quiet": 2, "still_at_brand": 3}


def _derive_status(current_company, known_brand):
    """Derive founder status from current_company and known_brand."""
    if not current_company:
        return "quiet"
    cc_lower = current_company.lower()
    # still at their famous brand
    if known_brand and known_brand.lower() in cc_lower:
        return "still_at_brand"
    # advisory / board roles
    advisory_keywords = ("advisor", "board", "investor", "venture", "capital",
                         "angel", "partner", "fund")
    if any(kw in cc_lower for kw in advisory_keywords):
        return "advisory"
    # actively building something new
    return "building"


@bp.route("/founder-profiles/import", methods=["POST"])
@admin_required()
def import_founder_profiles():
    """Bulk-upsert founder profiles (admin only).
    ---
    tags: [Admin]
    security:
      - Bearer: []
    requestBody:
      required: true
      content:
        application/json:
          schema:
            properties:
              profiles:
                type: array
    responses:
      200:
        description: Import result counts
    """
    body = request.get_json(silent=True) or {}
    profiles = body.get("profiles", [])
    if not isinstance(profiles, list) or not profiles:
        return jsonify({"error": "profiles must be a non-empty list"}), 400
    if len(profiles) > 2000:
        return jsonify({"error": "profiles list too large (max 2000)"}), 400

    imported = 0
    updated = 0
    now = datetime.utcnow()

    for p in profiles:
        name = (p.get("name") or "").strip()
        if not name:
            continue

        known_brand     = (p.get("brand") or p.get("known_brand") or "").strip() or None
        tier            = (p.get("tier") or "").strip().upper() or None
        current_company = (p.get("current_company") or "").strip() or None
        status          = (p.get("status") or "").strip() or None
        bio             = (p.get("bio") or "").strip() or None
        profile_url     = (p.get("profile_url") or "").strip() or None
        schools         = p.get("schools") or []
        past_companies  = p.get("past_companies") or []
        follower_count  = p.get("follower_count")
        x_handle        = (p.get("x_handle") or "").strip() or None

        # Derive status if not explicitly provided
        if not status:
            status = _derive_status(current_company, known_brand)

        existing = FounderProfile.query.filter_by(name=name).first()
        if existing:
            existing.known_brand     = known_brand
            existing.tier            = tier
            existing.current_company = current_company
            existing.status          = status
            existing.bio             = bio
            existing.profile_url     = profile_url
            existing.schools         = schools if isinstance(schools, list) else []
            existing.past_companies  = past_companies if isinstance(past_companies, list) else []
            existing.follower_count  = follower_count
            existing.x_handle        = x_handle
            existing.last_updated    = now
            updated += 1
        else:
            fp = FounderProfile(
                name=name,
                known_brand=known_brand,
                tier=tier,
                current_company=current_company,
                status=status,
                bio=bio,
                profile_url=profile_url,
                schools=schools if isinstance(schools, list) else [],
                past_companies=past_companies if isinstance(past_companies, list) else [],
                follower_count=follower_count,
                x_handle=x_handle,
                last_updated=now,
                created_at=now,
            )
            db.session.add(fp)
            imported += 1

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error("import_founder_profiles failed: %s", exc)
        return jsonify({"error": "Database error during import"}), 500

    return jsonify({"imported": imported, "updated": updated}), 200


@bp.route("/founder-profiles", methods=["GET"])
@admin_required()
def list_founder_profiles():
    """List founder profiles with optional filters (admin only).
    ---
    tags: [Admin]
    security:
      - Bearer: []
    parameters:
      - in: query
        name: tier
        schema: {type: string}
      - in: query
        name: status
        schema: {type: string}
      - in: query
        name: q
        schema: {type: string}
    responses:
      200:
        description: List of founder profiles
    """
    tier_filter     = request.args.get("tier", "").strip().upper() or None
    status_filter   = request.args.get("status", "").strip().lower() or None
    outreach_filter = request.args.get("outreach_status", "").strip().lower() or None
    q               = request.args.get("q", "").strip().lower() or None

    query = FounderProfile.query
    if tier_filter:
        query = query.filter(FounderProfile.tier == tier_filter)
    if status_filter:
        query = query.filter(FounderProfile.status == status_filter)
    if outreach_filter:
        query = query.filter(FounderProfile.outreach_status == outreach_filter)
    if q:
        query = query.filter(FounderProfile.name.ilike(f"%{q}%"))

    profiles = query.all()

    # Sort: tier order first, then status order within tier
    def sort_key(fp):
        tier_rank   = _TIER_ORDER.get(fp.tier or "", 99)
        status_rank = _STATUS_ORDER.get(fp.status or "", 99)
        return (tier_rank, status_rank, (fp.name or "").lower())

    profiles.sort(key=sort_key)

    return jsonify({"profiles": [fp.to_dict() for fp in profiles]}), 200


@bp.route("/founder-profiles/summary", methods=["GET"])
@admin_required()
def founder_profiles_summary():
    """Aggregate stats across all founder profiles (admin only).
    ---
    tags: [Admin]
    security:
      - Bearer: []
    responses:
      200:
        description: Summary stats
    """
    all_profiles = FounderProfile.query.all()
    total = len(all_profiles)

    building      = sum(1 for fp in all_profiles if fp.status == "building")
    advisory      = sum(1 for fp in all_profiles if fp.status == "advisory")
    quiet         = sum(1 for fp in all_profiles if fp.status == "quiet")
    still_at_brand = sum(1 for fp in all_profiles if fp.status == "still_at_brand")
    not_found     = sum(1 for fp in all_profiles if not fp.current_company and not fp.status)

    school_counter   = Counter()
    employer_counter = Counter()
    for fp in all_profiles:
        for school in (fp.schools or []):
            if school:
                school_counter[school] += 1
        for co in (fp.past_companies or []):
            if co:
                employer_counter[co] += 1

    top_schools          = school_counter.most_common(10)
    top_prior_employers  = employer_counter.most_common(10)

    # Outreach pipeline counts
    outreach_counts = Counter(fp.outreach_status or "cold" for fp in all_profiles)

    return jsonify({
        "total":            total,
        "building":         building,
        "advisory":         advisory,
        "quiet":            quiet,
        "still_at_brand":   still_at_brand,
        "not_found":        not_found,
        "top_schools":      top_schools,
        "top_prior_employers": top_prior_employers,
        "outreach": {
            "cold":             outreach_counts.get("cold", 0),
            "email_sent":       outreach_counts.get("email_sent", 0),
            "connected":        outreach_counts.get("connected", 0),
            "had_call":         outreach_counts.get("had_call", 0),
            "in_their_corner":  outreach_counts.get("in_their_corner", 0),
        },
    }), 200


@bp.route("/founder-profiles/<int:profile_id>", methods=["PATCH"])
@admin_required()
def update_founder_profile(profile_id):
    """Update outreach CRM fields on a founder profile (admin only).
    ---
    tags: [Admin]
    security:
      - Bearer: []
    """
    fp = FounderProfile.query.get_or_404(profile_id)
    data = request.get_json(silent=True) or {}

    allowed = {"outreach_status", "outreach_notes", "last_contact", "next_action"}
    for field in allowed:
        if field in data:
            if field == "last_contact":
                from datetime import date
                val = data[field]
                setattr(fp, field, date.fromisoformat(val) if val else None)
            else:
                setattr(fp, field, data[field])

    from ...extensions import db as _db
    _db.session.commit()
    return jsonify(fp.to_dict()), 200
