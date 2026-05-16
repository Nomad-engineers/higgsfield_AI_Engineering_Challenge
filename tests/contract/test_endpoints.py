"""Contract tests for all API endpoints.

Tests correct status codes, response shapes, and basic behavior.
"""
import pytest
import httpx

BASE_URL = "http://localhost:8080"


@pytest.fixture(scope="module")
def client():
    return httpx.Client(base_url=BASE_URL, timeout=30.0)


def _cleanup(client, user_id, session_id=None):
    client.delete(f"/users/{user_id}")
    if session_id:
        client.delete(f"/sessions/{session_id}")


class TestHealth:
    def test_health_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_has_status_field(self, client):
        body = resp.json() if (resp := client.get("/health")).status_code == 200 else {}
        if resp.status_code == 200:
            assert "status" in body


class TestTurns:
    def test_post_turns_returns_201(self, client):
        _cleanup(client, "ctest-user", "ctest-session")
        resp = client.post("/turns", json={
            "session_id": "ctest-session",
            "user_id": "ctest-user",
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
            ],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 201
        body = resp.json()
        assert "id" in body

    def test_post_turns_without_user_id(self, client):
        resp = client.post("/turns", json={
            "session_id": "ctest-anon",
            "user_id": None,
            "messages": [{"role": "user", "content": "Hello"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 201

    def test_post_turns_empty_messages_returns_422(self, client):
        resp = client.post("/turns", json={
            "session_id": "ctest-session",
            "user_id": "ctest-user",
            "messages": [],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 422


class TestRecall:
    def test_recall_returns_200(self, client):
        resp = client.post("/recall", json={
            "query": "test query",
            "session_id": "ctest-session",
            "user_id": "ctest-user",
            "max_tokens": 512,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert "context" in body
        assert "citations" in body

    def test_recall_missing_session_id_returns_422(self, client):
        resp = client.post("/recall", json={"query": "test", "user_id": "ctest-user"})
        assert resp.status_code == 422

    def test_recall_without_user_id(self, client):
        resp = client.post("/recall", json={
            "query": "test query",
            "session_id": "ctest-session",
            "user_id": None,
            "max_tokens": 512,
        })
        assert resp.status_code == 200


class TestSearch:
    def test_search_returns_200(self, client):
        resp = client.post("/search", json={
            "query": "test query",
            "user_id": "ctest-user",
            "session_id": "ctest-session",
            "limit": 5,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert "results" in body
        assert isinstance(body["results"], list)

    def test_search_with_query_only_returns_empty(self, client):
        resp = client.post("/search", json={"query": "test"})
        assert resp.status_code == 200
        assert resp.json()["results"] == []

    def test_search_missing_query_returns_422(self, client):
        resp = client.post("/search", json={})
        assert resp.status_code == 422


class TestMemories:
    def test_get_memories_returns_200(self, client):
        resp = client.get("/users/ctest-user/memories")
        assert resp.status_code == 200
        body = resp.json()
        assert "memories" in body
        assert isinstance(body["memories"], list)

    def test_get_memories_unknown_user_empty(self, client):
        resp = client.get("/users/nonexistent_user_xyz/memories")
        assert resp.status_code == 200
        body = resp.json()
        assert body["memories"] == []


class TestCleanup:
    def test_delete_session_returns_204(self, client):
        resp = client.delete("/sessions/ctest-session")
        assert resp.status_code == 204

    def test_delete_user_returns_204(self, client):
        resp = client.delete("/users/ctest-user")
        assert resp.status_code == 204

    def test_delete_nonexistent_returns_204(self, client):
        resp = client.delete("/users/does_not_exist_999")
        assert resp.status_code == 204
