from flask import Blueprint

bp = Blueprint("scans", __name__)

from . import routes  # noqa: E402, F401
