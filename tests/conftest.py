import pytest
from app import create_app
from app.extensions import db as _db


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
