"""
Webhook endpoints — authenticated with a shared secret (not JWT).

Used by the Claude Code scheduled task 'stealth-finder-inbox-audit' to push
brand names extracted from the Gmail Deals inbox into the Stealth Finder
without requiring a full user session.

Auth: Authorization: Bearer <INBOX_AUDIT_WEBHOOK_SECRET>
"""
import os
import logging

from flask import Blueprint, request, jsonify, current_app

bp = Blueprint('webhooks', __name__)
logger = logging.getLogger(__name__)


def _check_secret():
    expected = os.environ.get('INBOX_AUDIT_WEBHOOK_SECRET', '').strip()
    if not expected:
        logger.error("INBOX_AUDIT_WEBHOOK_SECRET not set in environment")
        return False
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return False
    return auth[7:].strip() == expected


@bp.route('/inbox-audit', methods=['POST'])
def inbox_audit_webhook():
    """
    POST /api/webhooks/inbox-audit

    Accepts a list of consumer brand names extracted from the Gmail Deals inbox
    and runs the Stealth Finder coverage audit — which brands from your deal
    flow did the scanner miss?

    Body: {"brand_names": ["Filament", "Stripes Beauty", ...]}
    Returns the full audit report (found, missing, coverage %).
    """
    if not _check_secret():
        return jsonify({'error': 'Unauthorized'}), 401

    body = request.get_json(silent=True) or {}
    brand_names = body.get('brand_names', [])

    if not isinstance(brand_names, list) or not brand_names:
        return jsonify({'error': 'brand_names must be a non-empty list'}), 400
    if len(brand_names) > 500:
        return jsonify({'error': 'brand_names list too large (max 500)'}), 400

    logger.info("Webhook inbox audit: %d brand names received", len(brand_names))

    from ...services.inbox_audit import check_brands_in_db
    app = current_app._get_current_object()
    result = check_brands_in_db(brand_names, app)
    return jsonify(result), 200
