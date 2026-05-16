"""E2E test for extraction pipeline.

Runs against the live Docker Compose stack with real OpenAI API.
Usage:
  docker compose up -d
  docker compose exec app python -m pytest tests/test_extraction_e2e.py -v
"""
import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = "http://localhost:8080"


@pytest.fixture(scope="module")
def client():
    return httpx.Client(base_url=BASE_URL, timeout=60.0)


def _cleanup(client, user_id):
    client.delete(f"/users/{user_id}")


def test_turn_extraction_e2e(client):
    """Full flow: POST /turns -> extraction -> memories at /users/{id}/memories."""
    _cleanup(client, "e2e-alice")

    resp = client.post("/turns", json={
        "session_id": "e2e-session",
        "user_id": "e2e-alice",
        "messages": [
            {"role": "user", "content": "My name is Alice and I live in Berlin"},
            {"role": "assistant", "content": "Nice to meet you!"},
        ],
        "timestamp": "2025-03-15T10:00:00Z",
    })
    assert resp.status_code == 201
    assert "id" in resp.json()

    resp = client.get("/users/e2e-alice/memories")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["memories"]) >= 1

    keys = {m["key"] for m in body["memories"]}
    assert any(k in keys for k in ["location", "name"])

    for m in body["memories"]:
        assert m["active"] is True
        assert m["confidence"] > 0

    _cleanup(client, "e2e-alice")


def test_turn_without_user_id(client):
    """When user_id is null, extraction is skipped."""
    resp = client.post("/turns", json={
        "session_id": "anon-session",
        "user_id": None,
        "messages": [{"role": "user", "content": "Hello"}],
        "timestamp": "2025-03-15T10:00:00Z",
    })
    assert resp.status_code == 201


def test_memories_empty_for_unknown_user(client):
    resp = client.get("/users/nonexistent/memories")
    assert resp.status_code == 200
    assert resp.json()["memories"] == []
