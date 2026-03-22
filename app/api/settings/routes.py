import os
import json
from flask import request, jsonify
from flask_jwt_extended import jwt_required
from . import bp
from ...extensions import db
from ...models.item import Item
from ...utils import db_get_user

SETTINGS_TITLE = "__bullish_settings__"

def _get_settings_item():
    return Item.query.filter_by(title=SETTINGS_TITLE).first()

def _default_settings():
    return {
        "_type": "settings",
        "alert_emails": [e.strip() for e in os.environ.get("ALERT_EMAILS", "").split(",") if e.strip()],
        "slack_webhook_url": os.environ.get("SLACK_WEBHOOK_URL", ""),
        "digest_enabled": True,
        "scan_days_back": 30,
        "scan_max_results": 200,
    }

@bp.route("", methods=["GET"])
@jwt_required()
def get_settings():
    item = _get_settings_item()
    if item:
        try:
            data = json.loads(item.description or "{}")
            data.pop("_type", None)
            return jsonify(data), 200
        except Exception:
            pass
    defaults = _default_settings()
    defaults.pop("_type", None)
    return jsonify(defaults), 200

@bp.route("", methods=["PATCH"])
@jwt_required()
def update_settings():
    user = db_get_user()
    if not user or not user.is_admin():
        return jsonify({"error": "Admin access required"}), 403

    data = request.get_json() or {}
    item = _get_settings_item()

    if item:
        try:
            current = json.loads(item.description or "{}")
        except Exception:
            current = {"_type": "settings"}
    else:
        current = {"_type": "settings"}

    allowed = {"alert_emails", "slack_webhook_url", "digest_enabled", "scan_days_back", "scan_max_results"}
    for key in allowed:
        if key in data:
            current[key] = data[key]

    if item:
        item.description = json.dumps(current)
    else:
        item = Item(title=SETTINGS_TITLE, owner_id=user.id, description=json.dumps(current))
        db.session.add(item)

    db.session.commit()
    result = {k: v for k, v in current.items() if k != "_type"}
    return jsonify(result), 200


@bp.route("/test-slack", methods=["POST"])
@jwt_required()
def test_slack():
    user = db_get_user()
    if not user or not user.is_admin():
        return jsonify({"error": "Admin access required"}), 403

    data = request.get_json() or {}
    webhook_url = data.get("webhook_url", "")
    if not webhook_url:
        return jsonify({"error": "webhook_url required"}), 400

    from ...services.slack import send_slack_test
    success = send_slack_test(webhook_url)
    if success:
        return jsonify({"message": "Test message sent successfully"}), 200
    return jsonify({"error": "Failed to send test message — check the webhook URL"}), 400
