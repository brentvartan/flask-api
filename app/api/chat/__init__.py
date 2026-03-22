from flask import Blueprint
bp = Blueprint("chat", __name__)
from . import routes  # noqa: F401, E402
