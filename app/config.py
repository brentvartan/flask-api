import os
from datetime import timedelta


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "jwt-change-me-in-production")
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=1)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=30)
    FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")
    MAIL_SUPPRESS_SEND = False


class DevelopmentConfig(Config):
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/flask_api_dev",
    )


class TestingConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    MAIL_SUPPRESS_SEND = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "TEST_DATABASE_URL",
        f"postgresql://{os.environ.get('USER', 'postgres')}@localhost:5432/flask_api_test",
    )


# ── Production secret fail-fast ───────────────────────────────────────────────
# The base Config keeps dev-friendly placeholder defaults so local dev and the
# test suite run with no secrets set. Production must never boot on a
# publicly-known signing key: if SECRET_KEY or JWT_SECRET_KEY is missing — or is
# still the placeholder — loading ProductionConfig raises instead of silently
# handing out forgeable JWTs.

_PLACEHOLDER_SECRETS = {
    "SECRET_KEY": "change-me-in-production",
    "JWT_SECRET_KEY": "jwt-change-me-in-production",
}


class _RequiredSecret:
    """Descriptor that resolves a secret from the environment, or raises.

    Flask's ``app.config.from_object(ProductionConfig)`` does a ``getattr`` for
    every uppercase attribute, so the check fires exactly when the production
    config is loaded in create_app() — not when this module is imported by the
    development or testing configs.
    """

    def __init__(self, env_var):
        self.env_var = env_var
        self.placeholder = _PLACEHOLDER_SECRETS[env_var]

    def __get__(self, obj, objtype=None):
        value = os.environ.get(self.env_var)
        if not value or value == self.placeholder:
            raise RuntimeError(
                f"{self.env_var} is unset or still the placeholder value in "
                f"production. Refusing to start on a publicly-known signing key — "
                f"set {self.env_var} in the Railway environment and redeploy."
            )
        return value


class ProductionConfig(Config):
    SECRET_KEY = _RequiredSecret("SECRET_KEY")
    JWT_SECRET_KEY = _RequiredSecret("JWT_SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL")


config = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}
