import os
import logging
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
from flask import Flask, jsonify
from flask_cors import CORS
from .config import config
from .extensions import db, migrate, jwt, bcrypt, limiter


def create_app(config_name=None):
    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "default")

    # Sentry — only initialised when SENTRY_DSN is present (not in dev/test)
    sentry_dsn = os.environ.get("SENTRY_DSN")
    if sentry_dsn:
        sentry_sdk.init(
            dsn=sentry_dsn,
            integrations=[FlaskIntegration()],
            traces_sample_rate=0.2,
            send_default_pii=False,
        )

    app = Flask(__name__)
    app.config.from_object(config[config_name])

    # Logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # CORS — allow requests from the frontend dev server and production domain
    CORS(app, resources={r"/api/*": {"origins": [
        "http://localhost:3000",
        "http://localhost:5173",
        os.environ.get("FRONTEND_URL", "http://localhost:5173"),
    ]}}, supports_credentials=True)

    # Init extensions
    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    bcrypt.init_app(app)
    limiter.init_app(app)

    # JWT token blocklist
    from .models.token_blocklist import TokenBlocklist

    @jwt.token_in_blocklist_loader
    def check_if_token_revoked(jwt_header, jwt_payload):
        jti = jwt_payload["jti"]
        return db.session.query(
            TokenBlocklist.query.filter_by(jti=jti).exists()
        ).scalar()

    # JWT error handlers
    @jwt.expired_token_loader
    def expired_token_callback(jwt_header, jwt_payload):
        return jsonify({"error": "Token has expired"}), 401

    @jwt.invalid_token_loader
    def invalid_token_callback(error):
        return jsonify({"error": "Invalid token"}), 401

    @jwt.unauthorized_loader
    def missing_token_callback(error):
        return jsonify({"error": "Authorization token required"}), 401

    @jwt.revoked_token_loader
    def revoked_token_callback(jwt_header, jwt_payload):
        return jsonify({"error": "Token has been revoked"}), 401

    # App-level error handlers
    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "Not found"}), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return jsonify({"error": "Method not allowed"}), 405

    @app.errorhandler(429)
    def rate_limit_exceeded(e):
        return jsonify({"error": "Rate limit exceeded. Try again later."}), 429

    @app.errorhandler(500)
    def internal_error(e):
        app.logger.error("500 error: %s", str(e))
        return jsonify({"error": "Internal server error"}), 500

    # Health check endpoint
    @app.route("/health")
    def health():
        return jsonify({"status": "ok"}), 200

    # Register blueprints
    from .api.auth import bp as auth_bp
    from .api.items import bp as items_bp
    from .api.admin import bp as admin_bp
    from .api.scans import bp as scans_bp

    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(items_bp, url_prefix="/api/items")
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    app.register_blueprint(scans_bp, url_prefix="/api/scans")

    # CLI commands
    from .cli import register_commands
    register_commands(app)

    # Swagger docs (dev/staging only — skip in production to avoid
    # Flasgger/Flask 3.x compatibility issues and YAML parse errors)
    if config_name != "production":
        from flasgger import Swagger
        Swagger(app, config={
            "headers": [],
            "specs": [{"endpoint": "apispec", "route": "/apispec.json"}],
            "static_url_path": "/flasgger_static",
            "swagger_ui": True,
            "specs_route": "/api/docs",
            "title": "Flask API",
            "version": "1.0.0",
            "securityDefinitions": {
                "Bearer": {
                    "type": "apiKey",
                    "name": "Authorization",
                    "in": "header",
                    "description": "JWT token: Bearer <token>",
                }
            },
        })

    return app
