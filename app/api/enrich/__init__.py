from flask import Blueprint

bp = Blueprint("enrich", __name__)

from . import routes  # noqa: F401, E402
