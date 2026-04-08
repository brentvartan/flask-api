from flask import jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity

from . import bp
from ...extensions import db, limiter
from ...models.scheduled_scan import ScheduledScan
from ...services.scheduler import run_scan_now

# Default scan seeded for every new user on first visit
_DEFAULT_SCAN = {
    "name":        "Daily USPTO Consumer Scan",
    "days_back":   7,
    "max_results": 200,
    "frequency":   "daily",
}

_VALID_SCAN_TYPES = ('full', 'trademark', 'delaware', 'producthunt', 'app_store')
_VALID_FREQUENCIES = ('daily', 'weekly')


@bp.route("/", methods=["GET"])
@jwt_required()
def list_scans():
    """List all scheduled scans for the current user (auto-seeds default if none)."""
    user_id = int(get_jwt_identity())
    scans = (
        ScheduledScan.query
        .filter_by(owner_id=user_id)
        .order_by(ScheduledScan.created_at)
        .all()
    )

    # Seed one default scan on first visit
    if not scans:
        default = ScheduledScan(owner_id=user_id, **_DEFAULT_SCAN)
        db.session.add(default)
        db.session.commit()
        scans = [default]

    return jsonify({"scans": [s.to_dict() for s in scans]}), 200


@bp.route("/", methods=["POST"])
@jwt_required()
def create_scan():
    """Create a new scheduled scan config."""
    user_id = int(get_jwt_identity())
    data = request.get_json(silent=True) or {}

    scan = ScheduledScan(
        owner_id=user_id,
        name=data.get("name", "New Scan"),
        days_back=max(1, min(int(data.get("days_back", 7)), 90)),
        max_results=max(10, min(int(data.get("max_results", 200)), 500)),
        frequency=data.get("frequency", "daily") if data.get("frequency") in _VALID_FREQUENCIES else "daily",
        enabled=bool(data.get("enabled", True)),
        scan_type=data.get("scan_type", "full") if data.get("scan_type") in _VALID_SCAN_TYPES else "full",
    )
    db.session.add(scan)
    db.session.commit()
    return jsonify(scan.to_dict()), 201


@bp.route("/<int:scan_id>", methods=["PATCH"])
@jwt_required()
def update_scan(scan_id):
    """Toggle enabled or update a scan's settings."""
    user_id = int(get_jwt_identity())
    scan = ScheduledScan.query.filter_by(id=scan_id, owner_id=user_id).first_or_404()

    data = request.get_json(silent=True) or {}
    if "name"       in data: scan.name      = data["name"]
    if "days_back"  in data: scan.days_back  = max(1,  min(int(data["days_back"]),  90))
    if "max_results" in data: scan.max_results = max(10, min(int(data["max_results"]), 500))
    if "enabled"    in data: scan.enabled   = bool(data["enabled"])
    if "frequency"  in data and data["frequency"]  in _VALID_FREQUENCIES:  scan.frequency  = data["frequency"]
    if "scan_type"  in data and data["scan_type"]   in _VALID_SCAN_TYPES:   scan.scan_type  = data["scan_type"]

    db.session.commit()
    return jsonify(scan.to_dict()), 200


@bp.route("/<int:scan_id>", methods=["DELETE"])
@jwt_required()
def delete_scan(scan_id):
    """Delete a scheduled scan config."""
    user_id = int(get_jwt_identity())
    scan = ScheduledScan.query.filter_by(id=scan_id, owner_id=user_id).first_or_404()
    db.session.delete(scan)
    db.session.commit()
    return jsonify({"deleted": True}), 200


@bp.route("/<int:scan_id>/run", methods=["POST"])
@jwt_required()
@limiter.limit("5 per minute")
def run_scan(scan_id):
    """Manually trigger a scheduled scan right now."""
    user_id = int(get_jwt_identity())
    scan = ScheduledScan.query.filter_by(id=scan_id, owner_id=user_id).first_or_404()

    # Allow manual run even if recently run (bypass the 1h cooldown)
    scan.last_run_at = None
    db.session.commit()

    result = run_scan_now(scan, user_id)
    return jsonify({**result, "scan": scan.to_dict()}), 200


@bp.route("/<int:scan_id>/runs", methods=["GET"])
@jwt_required()
def get_scan_runs(scan_id):
    """Return last 10 runs for a scan."""
    from ...models.scan_run import ScanRun
    user_id = int(get_jwt_identity())
    scan = ScheduledScan.query.filter_by(id=scan_id, owner_id=user_id).first_or_404()
    runs = (ScanRun.query
            .filter_by(scan_id=scan_id)
            .order_by(ScanRun.ran_at.desc())
            .limit(10)
            .all())
    return jsonify([r.to_dict() for r in runs]), 200
