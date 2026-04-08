from starlette.testclient import TestClient

from app.main import app


def test_index_returns_200():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200


def test_index_contains_title():
    client = TestClient(app)
    response = client.get("/")
    assert "Seadusloome" in response.text
