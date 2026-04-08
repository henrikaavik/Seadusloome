from starlette.testclient import TestClient

from app.main import app


def test_unauthenticated_index_redirects_to_login():
    client = TestClient(app, follow_redirects=False)
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_login_page_returns_200():
    client = TestClient(app)
    response = client.get("/auth/login")
    assert response.status_code == 200


def test_login_page_contains_form():
    client = TestClient(app)
    response = client.get("/auth/login")
    assert "Sisselogimine" in response.text
    assert 'name="email"' in response.text
    assert 'name="password"' in response.text
