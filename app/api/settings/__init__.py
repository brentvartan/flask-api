from flask import Blueprint
bp = Blueprint("settings", __name__)
from . import routes  # noqa: F401, E402
