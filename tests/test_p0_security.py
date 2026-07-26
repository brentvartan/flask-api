"""P0 security regressions — the four holes closed in the July 2026 audit.

1. Open self-registration on POST /api/auth/register
2. Missing ownership check on PUT /api/items/<id> (settings-singleton takeover)
3. Production booting on placeholder signing keys
4. Deactivated/deleted users keeping working tokens
"""
import json

import pytest
from flask import Config as FlaskConfig

from app.config import DevelopmentConfig, ProductionConfig, TestingConfig
from app.models.item import Item
from app.models.user import User


def make_item(client, token, title="Test Item", description="A description"):
    return client.post("/api/items", json={"title": title, "description": description},
                       headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def settings_item(db, admin_user):
    """The singleton row that holds alert_emails — the takeover target."""
    item = Item(
        title="__bullish_settings__",
        item_type="settings",
        owner_id=admin_user.id,
        description=json.dumps({"_type": "settings", "alert_emails": ["brent@bullish.co"]}),
    )
    db.session.add(item)
    db.session.commit()
    return item


# ── Fix 1: open self-registration ─────────────────────────────────────────────

class TestOpenRegistrationGate:
    REGISTRATION = {
        "email": "attacker@bullish.co",
        "password": "password123",
        "first_name": "Att",
        "last_name": "Acker",
    }

    def test_register_blocked_when_flag_off(self, client, db):
        resp = client.post("/api/auth/register", json=self.REGISTRATION)
        assert resp.status_code == 403
        assert "invite" in resp.get_json()["error"].lower()

    def test_blocked_register_creates_no_user(self, client, db):
        client.post("/api/auth/register", json=self.REGISTRATION)
        assert User.query.filter_by(email="attacker@bullish.co").first() is None

    def test_gate_fires_before_field_validation(self, client, db):
        """A partial payload must not get a different answer — no probing."""
        resp = client.post("/api/auth/register", json={"email": "x@bullish.co"})
        assert resp.status_code == 403

    def test_register_works_when_flag_explicitly_enabled(self, client, db, monkeypatch):
        monkeypatch.setenv("ENABLE_OPEN_REGISTRATION", "true")
        resp = client.post("/api/auth/register", json=self.REGISTRATION)
        assert resp.status_code == 201
        assert resp.get_json()["user"]["email"] == "attacker@bullish.co"

    def test_falsey_string_does_not_enable(self, client, db, monkeypatch):
        monkeypatch.setenv("ENABLE_OPEN_REGISTRATION", "false")
        resp = client.post("/api/auth/register", json=self.REGISTRATION)
        assert resp.status_code == 403


# ── Fix 2: item ownership ─────────────────────────────────────────────────────

class TestItemOwnership:
    def test_teammate_can_update_another_users_ordinary_item(self, client, user_token, admin_token):
        """
        Ordinary rows stay team-writable BY DESIGN — see the note in
        update_item. Watchlist entries are owned by whichever account ran the
        scan that auto-added them, every row is visible team-wide, and the UI
        lets any teammate mute or annotate them. Locking these to the owner
        would 403 every non-admin doing normal work. The singleton guard below
        is what actually closes the alert-hijack hole.
        """
        item_id = make_item(client, admin_token, "Admin item").get_json()["item"]["id"]
        resp = client.put(f"/api/items/{item_id}", json={"title": "Teammate edit"},
                          headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 200

    def test_owner_can_still_update_own_item(self, client, user_token):
        item_id = make_item(client, user_token).get_json()["item"]["id"]
        resp = client.put(f"/api/items/{item_id}", json={"title": "Updated"},
                          headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 200
        assert resp.get_json()["item"]["title"] == "Updated"

    def test_admin_can_still_update_any_item(self, client, user_token, admin_token):
        item_id = make_item(client, user_token).get_json()["item"]["id"]
        resp = client.put(f"/api/items/{item_id}", json={"title": "Admin edit"},
                          headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200


# ── Fix 2: protected singletons ───────────────────────────────────────────────

class TestProtectedSingletons:
    HIJACK = json.dumps({"_type": "settings", "alert_emails": ["attacker@evil.com"]})

    def test_regular_user_cannot_update_settings_row(self, client, db, user_token, settings_item):
        resp = client.put(f"/api/items/{settings_item.id}", json={"description": self.HIJACK},
                          headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 403

    def test_admin_cannot_update_settings_row_either(self, client, db, admin_token, settings_item):
        resp = client.put(f"/api/items/{settings_item.id}", json={"description": self.HIJACK},
                          headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 403

    def test_alert_emails_survive_the_attack(self, client, db, user_token, settings_item):
        item_id = settings_item.id
        client.put(f"/api/items/{item_id}", json={"description": self.HIJACK},
                   headers={"Authorization": f"Bearer {user_token}"})
        db.session.expire_all()
        stored = json.loads(db.session.get(Item, item_id).description)
        assert stored["alert_emails"] == ["brent@bullish.co"]

    def test_double_underscore_title_is_protected(self, client, db, admin_token, admin_user):
        """Any __-prefixed singleton, whatever its item_type."""
        row = Item(title="__scheduler_heartbeat__", item_type="system",
                   owner_id=admin_user.id, description="{}")
        db.session.add(row)
        db.session.commit()
        resp = client.put(f"/api/items/{row.id}", json={"description": self.HIJACK},
                          headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 403

    def test_cannot_create_a_row_that_shadows_the_settings_singleton(
        self, client, db, user_token, settings_item
    ):
        """Singleton lookups are filter_by(title).first() with no tiebreak, so a
        duplicate row is another way to hijack alert routing."""
        resp = client.post("/api/items",
                           json={"title": "__bullish_settings__", "description": self.HIJACK},
                           headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 403
        assert Item.query.filter_by(title="__bullish_settings__").count() == 1

    def test_bulk_import_skips_reserved_titles(self, client, db, user_token):
        resp = client.post("/api/items/bulk", json={"items": [
            {"title": "__bullish_settings__", "description": self.HIJACK},
            {"title": "Real Brand", "description": "{}"},
        ]}, headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 201
        assert resp.get_json()["created"] == 1
        assert Item.query.filter_by(title="__bullish_settings__").count() == 0

    def test_settings_row_cannot_be_deleted(self, client, db, admin_token, settings_item):
        resp = client.delete(f"/api/items/{settings_item.id}",
                             headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 403

    def test_dedicated_settings_endpoint_still_works(self, client, db, admin_token, settings_item):
        """The block must not lock admins out of the supported path."""
        resp = client.patch("/api/settings",
                            json={"alert_emails": ["brent@bullish.co", "ops@bullish.co"]},
                            headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200
        assert resp.get_json()["alert_emails"] == ["brent@bullish.co", "ops@bullish.co"]


# ── Fix 3: production secret fail-fast ────────────────────────────────────────

def _load(config_class):
    """Load a config class the way create_app() does, without booting an app."""
    cfg = FlaskConfig("/")
    cfg.from_object(config_class)
    return cfg


class TestProductionConfigSecrets:
    def test_raises_when_both_secrets_missing(self, monkeypatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
        with pytest.raises(RuntimeError):
            _load(ProductionConfig)

    def test_raises_when_only_secret_key_missing(self, monkeypatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.setenv("JWT_SECRET_KEY", "a-real-jwt-secret")
        with pytest.raises(RuntimeError) as exc:
            _load(ProductionConfig)
        assert "SECRET_KEY" in str(exc.value)

    def test_raises_when_only_jwt_secret_missing(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "a-real-secret")
        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
        with pytest.raises(RuntimeError) as exc:
            _load(ProductionConfig)
        assert "JWT_SECRET_KEY" in str(exc.value)

    def test_raises_on_placeholder_values(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "change-me-in-production")
        monkeypatch.setenv("JWT_SECRET_KEY", "jwt-change-me-in-production")
        with pytest.raises(RuntimeError):
            _load(ProductionConfig)

    def test_loads_when_both_secrets_present(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "prod-secret")
        monkeypatch.setenv("JWT_SECRET_KEY", "prod-jwt-secret")
        cfg = _load(ProductionConfig)
        assert cfg["SECRET_KEY"] == "prod-secret"
        assert cfg["JWT_SECRET_KEY"] == "prod-jwt-secret"

    def test_dev_and_test_configs_load_without_secrets(self, monkeypatch):
        """Local dev and CI must keep working with no secrets in the environment."""
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
        assert _load(TestingConfig)["SECRET_KEY"] == "change-me-in-production"
        assert _load(DevelopmentConfig)["JWT_SECRET_KEY"] == "jwt-change-me-in-production"


# ── Fix 4: revoked users lose their tokens ────────────────────────────────────

class TestDeactivatedUserTokens:
    def test_deactivated_user_access_token_rejected(self, client, db, regular_user, user_token):
        headers = {"Authorization": f"Bearer {user_token}"}
        assert client.get("/api/auth/me", headers=headers).status_code == 200

        regular_user.is_active = False
        db.session.commit()

        assert client.get("/api/auth/me", headers=headers).status_code == 401
        assert client.get("/api/items", headers=headers).status_code == 401

    def test_deactivated_user_refresh_token_rejected(self, client, db, regular_user):
        tokens = client.post("/api/auth/login", json={
            "email": "user@test.com", "password": "password123",
        }).get_json()

        regular_user.is_active = False
        db.session.commit()

        resp = client.post("/api/auth/refresh", headers={
            "Authorization": f"Bearer {tokens['refresh_token']}",
        })
        assert resp.status_code == 401

    def test_deleted_user_token_rejected(self, client, db, regular_user, user_token):
        headers = {"Authorization": f"Bearer {user_token}"}
        db.session.delete(regular_user)
        db.session.commit()
        assert client.get("/api/auth/me", headers=headers).status_code == 401

    def test_active_user_is_unaffected(self, client, admin_token):
        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200
