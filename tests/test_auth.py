import pytest


class TestRegister:
    def test_register_success(self, client, db):
        resp = client.post("/api/auth/register", json={
            "email": "new@test.com",
            "password": "password123",
            "first_name": "New",
            "last_name": "User",
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["user"]["email"] == "new@test.com"
        assert data["user"]["role"] == "user"
        assert "access_token" in data
        assert "refresh_token" in data

    def test_register_duplicate_email(self, client, regular_user):
        resp = client.post("/api/auth/register", json={
            "email": "user@test.com",
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
