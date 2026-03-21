from flask import Blueprint

bp = Blueprint("scheduled_scans", __name__)

from . import routes  # noqa: E402, F401
