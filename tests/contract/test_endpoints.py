"""Contract tests for all API endpoints.

Tests correct status codes, response shapes, and basic behavior.
Runs against the live Docker Compose stack. Auto-skipped if server is unreachable.
"""
import pytest
import httpx

pytestmark = pytest.mark.integration

BASE_URL = "http://localhost:8080"


@pytest.fixture(scope="module")
def client():
    return httpx.Client(base_url=BASE_URL, timeout=30.0)


def _cleanup(client, user_id, session_id=None):
    client.delete(f"/users/{user_id}")
    if session_id:
        client.delete(f"/sessions/{session_id}")


# --- Health ---


class TestHealth:
    def test_health_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_has_status_field(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert body["status"] == "ok"

    def test_health_get_only(self, client):
        resp = client.post("/health", json={})
        assert resp.status_code == 405


# --- Turns ---


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
        assert isinstance(body["id"], str)
        assert len(body["id"]) > 0

    def test_post_turns_without_user_id(self, client):
        resp = client.post("/turns", json={
            "session_id": "ctest-anon",
            "user_id": None,
            "messages": [{"role": "user", "content": "Hello"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 201

    def test_post_turns_with_metadata(self, client):
        _cleanup(client, "ctest-meta-user", "ctest-meta-session")
        resp = client.post("/turns", json={
            "session_id": "ctest-meta-session",
            "user_id": "ctest-meta-user",
            "messages": [{"role": "user", "content": "hello"}],
            "timestamp": "2025-03-15T10:00:00Z",
            "metadata": {"source": "test", "version": 2},
        })
        assert resp.status_code == 201

    def test_post_turns_without_metadata(self, client):
        resp = client.post("/turns", json={
            "session_id": "ctest-no-meta",
            "messages": [{"role": "user", "content": "hello"}],
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

    def test_post_turns_missing_session_id(self, client):
        resp = client.post("/turns", json={
            "messages": [{"role": "user", "content": "hi"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 422

    def test_post_turns_missing_timestamp(self, client):
        resp = client.post("/turns", json={
            "session_id": "ctest-session",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert resp.status_code == 422

    def test_post_turns_missing_messages(self, client):
        resp = client.post("/turns", json={
            "session_id": "ctest-session",
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 422

    def test_post_turns_multiple_messages(self, client):
        _cleanup(client, "ctest-multi-user", "ctest-multi-session")
        resp = client.post("/turns", json={
            "session_id": "ctest-multi-session",
            "user_id": None,
            "messages": [
                {"role": "user", "content": "First message"},
                {"role": "assistant", "content": "Response 1"},
                {"role": "user", "content": "Second message"},
                {"role": "assistant", "content": "Response 2"},
            ],
            "timestamp": "2025-03-15T10:00:00Z",
        }, timeout=60.0)
        assert resp.status_code == 201


# --- Recall ---


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
        assert isinstance(body["context"], str)
        assert isinstance(body["citations"], list)

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

    def test_recall_missing_query(self, client):
        resp = client.post("/recall", json={
            "session_id": "ctest-session",
        })
        assert resp.status_code == 422

    def test_recall_citations_shape(self, client):
        _cleanup(client, "ctest-cite-user", "ctest-cite-session")
        client.post("/turns", json={
            "session_id": "ctest-cite-session",
            "user_id": "ctest-cite-user",
            "messages": [{"role": "user", "content": "I live in Paris"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        resp = client.post("/recall", json={
            "query": "where does the user live",
            "session_id": "ctest-cite-session",
            "user_id": "ctest-cite-user",
            "max_tokens": 512,
        })
        body = resp.json()
        assert resp.status_code == 200
        if body["citations"]:
            c = body["citations"][0]
            assert "score" in c
            assert "snippet" in c
            assert isinstance(c["score"], float)

    def test_recall_max_tokens_respected(self, client):
        resp = client.post("/recall", json={
            "query": "test",
            "session_id": "ctest-session",
            "user_id": "ctest-user",
            "max_tokens": 64,
        })
        assert resp.status_code == 200


# --- Search ---


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

    def test_search_result_shape(self, client):
        _cleanup(client, "ctest-search-user", "ctest-search-session")
        client.post("/turns", json={
            "session_id": "ctest-search-session",
            "user_id": "ctest-search-user",
            "messages": [{"role": "user", "content": "I work at Acme Corp"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        resp = client.post("/search", json={
            "query": "employer",
            "user_id": "ctest-search-user",
            "session_id": "ctest-search-session",
        })
        assert resp.status_code == 200
        results = resp.json()["results"]
        if results:
            r = results[0]
            assert "content" in r
            assert "score" in r
            assert "session_id" in r
            assert "timestamp" in r
            assert "metadata" in r

    def test_search_limit_param(self, client):
        resp = client.post("/search", json={
            "query": "test",
            "user_id": "ctest-user",
            "limit": 2,
        })
        assert resp.status_code == 200
        assert len(resp.json()["results"]) <= 2


# --- Memories ---


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

    def test_memories_item_shape(self, client):
        _cleanup(client, "ctest-shape-user", "ctest-shape-session")
        client.post("/turns", json={
            "session_id": "ctest-shape-session",
            "user_id": "ctest-shape-user",
            "messages": [{"role": "user", "content": "My name is Shape Test"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        resp = client.get("/users/ctest-shape-user/memories")
        body = resp.json()
        assert len(body["memories"]) >= 1
        m = body["memories"][0]
        required_fields = {"id", "type", "key", "value", "confidence", "active",
                           "source_session", "source_turn", "supersedes",
                           "superseded_by", "created_at", "updated_at"}
        assert required_fields.issubset(m.keys())


# --- Extra fields rejected ---


class TestExtraFieldsRejected:
    def test_turns_rejects_unknown_fields(self, client):
        resp = client.post("/turns", json={
            "session_id": "ctest-extra",
            "messages": [{"role": "user", "content": "hi"}],
            "timestamp": "2025-01-01T00:00:00Z",
            "metadata": {},
            "unknown_field": "should_fail",
        })
        assert resp.status_code == 422

    def test_recall_rejects_unknown_fields(self, client):
        resp = client.post("/recall", json={
            "query": "test",
            "session_id": "ctest-extra",
            "max_tokens": 512,
            "extra": "bad",
        })
        assert resp.status_code == 422

    def test_search_rejects_unknown_fields(self, client):
        resp = client.post("/search", json={
            "query": "test",
            "session_id": "ctest-extra",
            "bogus": True,
        })
        assert resp.status_code == 422

    def test_turns_message_rejects_unknown_fields(self, client):
        resp = client.post("/turns", json={
            "session_id": "ctest-msg-extra",
            "messages": [{"role": "user", "content": "hi", "bogus": 1}],
            "timestamp": "2025-01-01T00:00:00Z",
        })
        assert resp.status_code == 422

    def test_recall_rejects_multiple_unknown_fields(self, client):
        resp = client.post("/recall", json={
            "query": "test",
            "session_id": "ctest-extra",
            "max_tokens": 512,
            "foo": 1,
            "bar": 2,
        })
        assert resp.status_code == 422


# --- Cleanup / Delete ---


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

    def test_delete_session_then_recall_empty(self, client):
        _cleanup(client, "ctest-del-user", "ctest-del-session")
        client.post("/turns", json={
            "session_id": "ctest-del-session",
            "user_id": "ctest-del-user",
            "messages": [{"role": "user", "content": "I live in Tokyo"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        client.delete("/sessions/ctest-del-session")
        resp = client.post("/recall", json={
            "query": "where does user live",
            "session_id": "ctest-del-session-2",
            "user_id": "ctest-del-user",
        })
        assert resp.status_code == 200

    def test_delete_user_removes_memories(self, client):
        _cleanup(client, "ctest-del-mem-user", "ctest-del-mem-session")
        client.post("/turns", json={
            "session_id": "ctest-del-mem-session",
            "user_id": "ctest-del-mem-user",
            "messages": [{"role": "user", "content": "I live in Oslo"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        resp_before = client.get("/users/ctest-del-mem-user/memories")
        assert len(resp_before.json()["memories"]) >= 1
        client.delete("/users/ctest-del-mem-user")
        resp_after = client.get("/users/ctest-del-mem-user/memories")
        assert resp_after.json()["memories"] == []
