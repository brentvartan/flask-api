import pytest
from app import create_app
from app.extensions import db as _db


# ── No test may ever spend money ──────────────────────────────────────────────
# Autouse and session-wide, so it covers every test including ones written later.
#
# The enrichment path calls Anthropic in two places now: enrich_signal (Sonnet)
# and, since P2, triage_signal (Haiku) which runs inside the shared pipeline on
# every saved trademark. Any pipeline test that forgets to patch BOTH would issue
# real, billable calls on a developer machine that happens to have the key
# exported — silently, and once per signal.
#
# Clearing the credentials makes _get_client() raise, which both stages already
# handle: triage fails OPEN (worth_scoring=True, no network) and enrich_signal
# returns {"enriched": False, ...}. Tests that need a working client patch
# _get_client directly, which takes precedence over this.
_PAID_API_KEYS = (
    "ANTHROPIC_API_KEY",
    "SERPAPI_API_KEY",
    "PROXYCURL_API_KEY",
    "RESEND_API_KEY",
)


@pytest.fixture(autouse=True, scope="session")
def _no_paid_api_calls():
    import os

    saved = {k: os.environ.pop(k, None) for k in _PAID_API_KEYS}
    yield
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v


@pytest.fixture(scope="session")
def app():
    app = create_app("testing")
    with app.app_context():
        _db.create_all()
        yield app
        _db.drop_all()


@pytest.fixture(scope="function")
def db(app):
    with app.app_context():
        yield _db
        _db.session.rollback()
        # Clean tables between tests
        for table in reversed(_db.metadata.sorted_tables):
            _db.session.execute(table.delete())
        _db.session.commit()


@pytest.fixture(scope="function")
def client(app):
    return app.test_client()


@pytest.fixture
def regular_user(db):
    from app.models.user import User
    user = User(email="user@test.com", first_name="Test", last_name="User", role="user")
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def admin_user(db):
    from app.models.user import User
    user = User(email="admin@test.com", first_name="Admin", last_name="User", role="admin")
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def user_token(client, regular_user):
    resp = client.post("/api/auth/login", json={
        "email": "user@test.com", "password": "password123"
    })
    return resp.get_json()["access_token"]


@pytest.fixture
def admin_token(client, admin_user):
    resp = client.post("/api/auth/login", json={
        "email": "admin@test.com", "password": "password123"
    })
    return resp.get_json()["access_token"]
