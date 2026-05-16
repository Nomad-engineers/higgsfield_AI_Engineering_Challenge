"""Concurrent sessions test: no cross-user data bleeding."""
import httpx
import pytest

BASE_URL = "http://localhost:8080"


@pytest.fixture(scope="module")
def client():
    return httpx.Client(base_url=BASE_URL, timeout=30.0)


@pytest.fixture(autouse=True)
def cleanup(client):
    yield
    client.delete("/users/concurrent-user-a")
    client.delete("/users/concurrent-user-b")
    client.delete("/sessions/concurrent-sess-a")
    client.delete("/sessions/concurrent-sess-b")


def test_no_cross_user_bleed(client):
    # User A: lives in Tokyo
    resp = client.post("/turns", json={
        "session_id": "concurrent-sess-a",
        "user_id": "concurrent-user-a",
        "messages": [
            {"role": "user", "content": "I live in Tokyo and I love sushi"},
            {"role": "assistant", "content": "Tokyo is amazing!"},
        ],
        "timestamp": "2025-03-15T10:00:00Z",
    })
    assert resp.status_code == 201

    # User B: lives in London
    resp = client.post("/turns", json={
        "session_id": "concurrent-sess-b",
        "user_id": "concurrent-user-b",
        "messages": [
            {"role": "user", "content": "I live in London and I work at the BBC"},
            {"role": "assistant", "content": "London is wonderful!"},
        ],
        "timestamp": "2025-03-15T10:00:00Z",
    })
    assert resp.status_code == 201

    # Recall for user A should NOT mention London or BBC
    resp = client.post("/recall", json={
        "query": "where does this user live",
        "session_id": "concurrent-sess-a",
        "user_id": "concurrent-user-a",
        "max_tokens": 512,
    })
    assert resp.status_code == 200
    context_a = resp.json()["context"].lower()
    assert "tokyo" in context_a
    assert "london" not in context_a
    assert "bbc" not in context_a

    # Recall for user B should NOT mention Tokyo or sushi
    resp = client.post("/recall", json={
        "query": "where does this user live",
        "session_id": "concurrent-sess-b",
        "user_id": "concurrent-user-b",
        "max_tokens": 512,
    })
    assert resp.status_code == 200
    context_b = resp.json()["context"].lower()
    assert "london" in context_b
    assert "tokyo" not in context_b
    assert "sushi" not in context_b

    # Memories should be separate per user
    resp_a = client.get("/users/concurrent-user-a/memories")
    resp_b = client.get("/users/concurrent-user-b/memories")
    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    assert len(resp_a.json()["memories"]) > 0
    assert len(resp_b.json()["memories"]) > 0
