import pytest
from app.services.tokens import generate_reset_token


class TestRegister:
    def test_register_success(self, client, db):
        resp = client.post("/api/auth/register", json={
            "email": "new@bullish.co",
            "password": "password123",
            "first_name": "New",
            "last_name": "User",
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["user"]["email"] == "new@bullish.co"
        assert data["user"]["role"] == "user"
        assert "access_token" in data
        assert "refresh_token" in data

    def test_register_duplicate_email(self, client, db):
        # Pre-create a bullish.co user via the register route, then try again
        client.post("/api/auth/register", json={
            "email": "dupe@bullish.co",
            "password": "password123",
            "first_name": "First",
            "last_name": "User",
        })
        resp = client.post("/api/auth/register", json={
            "email": "dupe@bullish.co",
            "password": "password123",
            "first_name": "Dupe",
            "last_name": "User",
        })
        assert resp.status_code == 409

    def test_register_missing_fields(self, client, db):
        resp = client.post("/api/auth/register", json={"email": "x@test.com"})
        assert resp.status_code == 400


class TestLogin:
    def test_login_success(self, client, regular_user):
        resp = client.post("/api/auth/login", json={
            "email": "user@test.com", "password": "password123"
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert "access_token" in data

    def test_login_wrong_password(self, client, regular_user):
        resp = client.post("/api/auth/login", json={
            "email": "user@test.com", "password": "wrongpass"
        })
        assert resp.status_code == 401

    def test_login_unknown_email(self, client, db):
        resp = client.post("/api/auth/login", json={
            "email": "nobody@test.com", "password": "password123"
        })
        assert resp.status_code == 401


class TestMe:
    def test_me_authenticated(self, client, user_token):
        resp = client.get("/api/auth/me",
                          headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 200
        assert resp.get_json()["user"]["email"] == "user@test.com"

    def test_me_no_token(self, client):
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401


class TestLogout:
    def test_logout_revokes_token(self, client, user_token):
        resp = client.post("/api/auth/logout",
                           headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 200

        # Token should now be rejected
        resp2 = client.get("/api/auth/me",
                           headers={"Authorization": f"Bearer {user_token}"})
        assert resp2.status_code == 401


class TestRefreshTokenLogout:
    def test_logout_with_refresh_token_blocks_refresh(self, client, regular_user):
        """Logout with refresh token in body — subsequent refresh is blocked."""
        login = client.post("/api/auth/login", json={
            "email": "user@test.com", "password": "password123"
        })
        tokens = login.get_json()
        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]

        resp = client.post(
            "/api/auth/logout",
            json={"refresh_token": refresh_token},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert resp.status_code == 200

        # Refresh token should now be rejected
        resp2 = client.post(
            "/api/auth/refresh",
            headers={"Authorization": f"Bearer {refresh_token}"},
        )
        assert resp2.status_code == 401

    def test_logout_without_refresh_token_still_succeeds(self, client, user_token):
        """Backward-compat: logout with no body still works."""
        resp = client.post(
            "/api/auth/logout",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 200

    def test_logout_with_garbage_refresh_token_still_succeeds(self, client, user_token):
        """A garbage refresh_token string should not cause a 500."""
        resp = client.post(
            "/api/auth/logout",
            json={"refresh_token": "not-a-real-token"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 200

    def test_valid_refresh_token_returns_new_access_token(self, client, regular_user):
        """Refresh endpoint returns a new access token when not logged out."""
        login = client.post("/api/auth/login", json={
            "email": "user@test.com", "password": "password123"
        })
        refresh_token = login.get_json()["refresh_token"]

        resp = client.post(
            "/api/auth/refresh",
            headers={"Authorization": f"Bearer {refresh_token}"},
        )
        assert resp.status_code == 200
        assert "access_token" in resp.get_json()


class TestPasswordReset:
    def test_forgot_password_known_email_returns_200(self, client, regular_user, monkeypatch):
        monkeypatch.setenv("MAIL_SUPPRESS_SEND", "true")
        resp = client.post("/api/auth/forgot-password", json={"email": "user@test.com"})
        assert resp.status_code == 200
        assert "reset link" in resp.get_json()["message"]

    def test_forgot_password_unknown_email_still_returns_200(self, client, db, monkeypatch):
        """Must not reveal whether the email is registered (anti-enumeration)."""
        monkeypatch.setenv("MAIL_SUPPRESS_SEND", "true")
        resp = client.post("/api/auth/forgot-password", json={"email": "nobody@test.com"})
        assert resp.status_code == 200

    def test_forgot_password_invalid_email_format_returns_422(self, client, db):
        resp = client.post("/api/auth/forgot-password", json={"email": "not-an-email"})
        assert resp.status_code == 422

    def test_reset_password_success(self, client, app, regular_user):
        token = generate_reset_token(app.config["SECRET_KEY"], regular_user.id)
        resp = client.post("/api/auth/reset-password", json={
            "token": token, "password": "newpassword123",
        })
        assert resp.status_code == 200

        # New password should work
        login = client.post("/api/auth/login", json={
            "email": "user@test.com", "password": "newpassword123"
        })
        assert login.status_code == 200

    def test_reset_password_invalid_token_returns_400(self, client, db):
        resp = client.post("/api/auth/reset-password", json={
            "token": "completely.invalid.token", "password": "newpassword123",
        })
        assert resp.status_code == 400
        assert "Invalid" in resp.get_json()["error"]

    def test_reset_password_short_password_returns_422(self, client, app, regular_user):
        token = generate_reset_token(app.config["SECRET_KEY"], regular_user.id)
        resp = client.post("/api/auth/reset-password", json={
            "token": token, "password": "short",
        })
        assert resp.status_code == 422
