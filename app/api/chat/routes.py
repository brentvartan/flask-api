import logging
from flask import request, jsonify
from flask_jwt_extended import jwt_required
from . import bp
from ...services.chat import ask_bullish

logger = logging.getLogger(__name__)

@bp.route("/ask", methods=["POST"])
@jwt_required()
def ask():
    data = request.get_json() or {}
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"error": "messages array required"}), 400
    try:
        reply = ask_bullish(messages)
        return jsonify({"reply": reply}), 200
    except Exception as e:
        logger.error("ask_bullish failed: %s", repr(e), exc_info=True)
        return jsonify({"error": str(e)}), 500
