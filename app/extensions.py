import os
import logging
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_jwt_extended import JWTManager, get_jwt_identity
from flask_bcrypt import Bcrypt
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

logger = logging.getLogger(__name__)

db = SQLAlchemy()
migrate = Migrate()
jwt = JWTManager()
bcrypt = Bcrypt()

_redis_url = os.environ.get("REDIS_URL")
if not _redis_url:
    logger.warning(
        "REDIS_URL not set — rate limiting falls back to in-memory storage "
        "and will not work correctly across multiple gunicorn workers."
    )

def _get_rate_limit_key():
    """Key scan/enrich endpoints by JWT identity (user ID) when available,
    falling back to IP for unauthenticated routes."""
    try:
        identity = get_jwt_identity()
        if identity:
            return f"user:{identity}"
    except Exception:
        pass
    return get_remote_address()


limiter = Limiter(
    key_func=_get_rate_limit_key,
    default_limits=["200 per day", "60 per hour"],
    storage_uri=_redis_url or "memory://",
)
