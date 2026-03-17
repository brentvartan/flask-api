class TestAdminUsers:
    def test_admin_can_list_users(self, client, admin_token, regular_user):
        resp = client.get("/api/admin/users",
                          headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["pagination"]["total"] >= 1

    def test_non_admin_blocked(self, client, user_token):
        resp = client.get("/api/admin/users",
                          headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 403

    def test_unauthenticated_blocked(self, client):
        resp = client.get("/api/admin/users")
        assert resp.status_code == 401

    def test_admin_deactivate_user(self, client, admin_token, regular_user):
        resp = client.patch(f"/api/admin/users/{regular_user.id}",
                            json={"is_active": False},
                            headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200
        assert resp.get_json()["user"]["is_active"] is False

    def test_admin_reactivate_user(self, client, admin_token, regular_user):
        client.patch(f"/api/admin/users/{regular_user.id}",
                     json={"is_active": False},
                     headers={"Authorization": f"Bearer {admin_token}"})
        resp = client.patch(f"/api/admin/users/{regular_user.id}",
                            json={"is_active": True},
                            headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200
        assert resp.get_json()["user"]["is_active"] is True
