import pytest


def make_item(client, token, title="Test Item", description="A description"):
    resp = client.post("/api/items", json={"title": title, "description": description},
                       headers={"Authorization": f"Bearer {token}"})
    return resp


class TestCreateItem:
    def test_create_success(self, client, user_token):
        resp = make_item(client, user_token)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["item"]["title"] == "Test Item"

    def test_create_missing_title(self, client, user_token):
        resp = client.post("/api/items", json={"description": "no title"},
                           headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 422

    def test_create_unauthenticated(self, client):
        resp = client.post("/api/items", json={"title": "oops"})
        assert resp.status_code == 401


class TestListItems:
    def test_list_own_items(self, client, user_token):
        make_item(client, user_token, "Item 1")
        make_item(client, user_token, "Item 2")
        resp = client.get("/api/items", headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["pagination"]["total"] == 2
        assert len(data["items"]) == 2

    def test_pagination(self, client, user_token):
        for i in range(5):
            make_item(client, user_token, f"Item {i}")
        resp = client.get("/api/items?per_page=2&page=1",
                          headers={"Authorization": f"Bearer {user_token}"})
        data = resp.get_json()
        assert len(data["items"]) == 2
        assert data["pagination"]["has_next"] is True


class TestGetItem:
    def test_get_own_item(self, client, user_token):
        item_id = make_item(client, user_token).get_json()["item"]["id"]
        resp = client.get(f"/api/items/{item_id}",
                          headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 200

    def test_get_other_users_item(self, client, user_token, admin_token):
        # Items are shared across all authenticated team members — any user can read any item
        item_id = make_item(client, admin_token, "Admin item").get_json()["item"]["id"]
        resp = client.get(f"/api/items/{item_id}",
                          headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 200

    def test_get_nonexistent(self, client, user_token):
        resp = client.get("/api/items/99999",
                          headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 404


class TestUpdateItem:
    def test_update_own_item(self, client, user_token):
        item_id = make_item(client, user_token).get_json()["item"]["id"]
        resp = client.put(f"/api/items/{item_id}", json={"title": "Updated"},
                          headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 200
        assert resp.get_json()["item"]["title"] == "Updated"

    def test_update_other_users_item(self, client, user_token, admin_token):
        # Items are shared across all authenticated team members — any user can update any item
        item_id = make_item(client, admin_token).get_json()["item"]["id"]
        resp = client.put(f"/api/items/{item_id}", json={"title": "Steal"},
                          headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 200


class TestDeleteItem:
    def test_owner_can_delete(self, client, user_token):
        item_id = make_item(client, user_token).get_json()["item"]["id"]
        resp = client.delete(f"/api/items/{item_id}",
                             headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 200

    def test_admin_can_delete_any(self, client, user_token, admin_token):
        item_id = make_item(client, user_token).get_json()["item"]["id"]
        resp = client.delete(f"/api/items/{item_id}",
                             headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200

    def test_non_owner_cannot_delete(self, client, user_token, admin_token):
        item_id = make_item(client, admin_token).get_json()["item"]["id"]
        resp = client.delete(f"/api/items/{item_id}",
                             headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 403
