from flask import jsonify, request
from flask_jwt_extended import jwt_required
from . import bp
from ...services.trademarks import search_recent_trademarks


@bp.route("/trademark", methods=["POST"])
@jwt_required()
def run_trademark_scan():
    """
    Run a real-time USPTO trademark scan.

    Request body (all optional):
        days_back   int  Days of history to fetch (7–90, default 30)
        max_results int  Max signals to return (1–500, default 200)

    Returns:
        {
            "signals":     [...],
            "total_found": int,
            "fetched":     int,
            "error":       null | str
        }
    """
    data = request.get_json(silent=True) or {}

    days_back = int(data.get("days_back", 30))
    days_back = max(7, min(days_back, 90))

    max_results = int(data.get("max_results", 200))
    max_results = max(1, min(max_results, 500))

    result = search_recent_trademarks(
        days_back=days_back,
        max_results=max_results,
    )

    status = 200 if result.get("error") is None else 502
    return jsonify(result), status
