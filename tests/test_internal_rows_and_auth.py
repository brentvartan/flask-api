"""
Authorization boundaries around internal control rows and deactivated accounts.

Context: signal items are deliberately shared across the whole team — any member can
read and edit any signal (see tests/test_items.py::test_update_other_users_item, and
SignalDetail/MatchCard/Watchlist all call items.update on rows they don't own). That
sharing is correct and must keep working.

What was NOT correct is that the same table also stores internal control rows:
  __bullish_settings__     — slack_webhook_url + alert_emails
  __jlock_<job_id>__       — locks that gate the nightly scan
  __scheduler_heartbeat__  — scheduler liveness

Because those are ordinary Item rows, generic item CRUD was a way around the admin
check and field whitelist on /api/settings: any authenticated user could read the
Slack webhook, repoint every alert email, or write a future ran_at into a job lock
to suppress the nightly scan.
"""
import json

import pytest

from app.extensions import db as _db
from app.models.item import Item

SETTINGS_TITLE = "__bullish_settings__"
LOCK_TITLE = "__jlock_full_sweep__"
HEARTBEAT_TITLE = "__scheduler_heartbeat__"


@pytest.fixture
def internal_rows(db, admin_user, app):
    """The three internal control-row shapes, owned by the admin."""
    with app.app_context():
        rows = [
            Item(title=SETTINGS_TITLE, owner_id=admin_user.id, item_type="settings",
                 description=json.dumps({
                     "_type": "settings",
                     "slack_webhook_url": "https://hooks.slack.com/services/SECRET",
                     "alert_emails": ["brent@bullish.co"],
                 })),
            Item(title=LOCK_TITLE, owner_id=admin_user.id, item_type="system",
                 description=json.dumps({"ran_at": "2026-07-25T00:00:00+00:00"})),
            Item(title=HEARTBEAT_TITLE, owner_id=admin_user.id, item_type="system",
                 description=json.dumps({"last_run": "2026-07-25T06:00:00+00:00"})),
        ]
        _db.session.add_all(rows)
        _db.session.commit()
        return {r.title: r.id for r in rows}


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ── Internal rows are invisible through the generic items API ─────────────────

def test_list_items_hides_internal_rows(client, user_token, internal_rows):
    resp = client.get("/api/items?per_page=100", headers=_auth(user_token))
    assert resp.status_code == 200
    titles = [i["title"] for i in resp.get_json()["items"]]
    for t in (SETTINGS_TITLE, LOCK_TITLE, HEARTBEAT_TITLE):
        assert t not in titles, f"{t} leaked through GET /api/items"


def test_list_items_still_returns_shared_signals(client, user_token, admin_token):
    """The guard must not break team-wide sharing of real signals."""
    made = client.post("/api/items", json={"title": "SharedBrand", "description": "{}"},
                       headers=_auth(admin_token))
    assert made.status_code == 201
    resp = client.get("/api/items?per_page=100", headers=_auth(user_token))
    titles = [i["title"] for i in resp.get_json()["items"]]
    assert "SharedBrand" in titles, "team sharing of ordinary signals regressed"


def test_get_internal_row_by_id_is_404(client, user_token, internal_rows):
    """Webhook must not be readable by id even when the id is known."""
    resp = client.get(f"/api/items/{internal_rows[SETTINGS_TITLE]}", headers=_auth(user_token))
    assert resp.status_code == 404
    assert "SECRET" not in resp.get_data(as_text=True)


def test_put_cannot_rewrite_settings_row(client, user_token, internal_rows, app):
    """The escalation path: rewrite settings via item CRUD, dodging the admin gate."""
    resp = client.put(
        f"/api/items/{internal_rows[SETTINGS_TITLE]}",
        json={"description": json.dumps({
            "_type": "settings",
            "alert_emails": ["attacker@example.com"],
            "slack_webhook_url": "https://hooks.slack.com/services/ATTACKER",
        })},
        headers=_auth(user_token),
    )
    assert resp.status_code == 404

    with app.app_context():
        stored = json.loads(_db.session.get(Item, internal_rows[SETTINGS_TITLE]).description)
    assert stored["alert_emails"] == ["brent@bullish.co"]
    assert "ATTACKER" not in stored["slack_webhook_url"]


def test_put_cannot_suppress_nightly_scan_via_job_lock(client, user_token, internal_rows, app):
    """Writing a future ran_at into the lock would silently stop the nightly scan."""
    resp = client.put(
        f"/api/items/{internal_rows[LOCK_TITLE]}",
        json={"description": json.dumps({"ran_at": "2099-01-01T00:00:00+00:00"})},
        headers=_auth(user_token),
    )
    assert resp.status_code == 404
    with app.app_context():
        stored = json.loads(_db.session.get(Item, internal_rows[LOCK_TITLE]).description)
    assert stored["ran_at"].startswith("2026-")


def test_delete_cannot_remove_internal_rows(client, admin_token, internal_rows, app):
    """Even an admin shouldn't drop control rows through the generic CRUD path."""
    resp = client.delete(f"/api/items/{internal_rows[HEARTBEAT_TITLE]}", headers=_auth(admin_token))
    assert resp.status_code == 404
    with app.app_context():
        assert _db.session.get(Item, internal_rows[HEARTBEAT_TITLE]) is not None


# ── /api/settings read is admin-only ──────────────────────────────────────────

def test_get_settings_requires_admin(client, user_token, internal_rows):
    resp = client.get("/api/settings", headers=_auth(user_token))
    assert resp.status_code == 403
    assert "SECRET" not in resp.get_data(as_text=True)


def test_get_settings_allows_admin(client, admin_token, internal_rows):
    resp = client.get("/api/settings", headers=_auth(admin_token))
    assert resp.status_code == 200
    assert resp.get_json()["slack_webhook_url"].endswith("SECRET")


# ── Deactivation revokes access immediately ───────────────────────────────────

def test_deactivated_user_token_stops_working(client, regular_user, user_token, app):
    """
    Deactivating in Settings -> Team previously revoked nothing: the JWT loader
    checked only the jti blocklist, so an existing access token kept working and the
    30-day refresh token could keep minting new ones.
    """
    ok = client.get("/api/auth/me", headers=_auth(user_token))
    assert ok.status_code == 200

    # Mutate through the session the request will actually use. The conftest `db`
    # fixture already holds an app context, and Flask's test client reuses it rather
    # than pushing a new one — so a nested app_context() here would commit from a
    # SECOND session and leave this one's cached User stale at is_active=True,
    # failing the assertion below for reasons that have nothing to do with the app.
    from app.models.user import User
    user = _db.session.get(User, regular_user.id)
    user.is_active = False
    _db.session.commit()

    after = client.get("/api/auth/me", headers=_auth(user_token))
    assert after.status_code in (401, 422), (
        "a deactivated user's existing access token still works"
    )


def test_active_user_unaffected(client, user_token):
    assert client.get("/api/auth/me", headers=_auth(user_token)).status_code == 200
