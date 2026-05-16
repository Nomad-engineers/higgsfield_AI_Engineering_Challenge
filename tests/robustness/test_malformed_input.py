"""Robustness tests: malformed input, unicode, edge cases."""
import httpx
import pytest

BASE_URL = "http://localhost:8080"


@pytest.fixture(scope="module")
def client():
    return httpx.Client(base_url=BASE_URL, timeout=30.0)


class TestMalformedInput:
    def test_invalid_json_body(self, client):
        resp = client.post(
            "/turns",
            content="not json at all{{{",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    def test_missing_required_fields(self, client):
        resp = client.post("/turns", json={"session_id": "x"})
        assert resp.status_code == 422

    def test_empty_messages_array(self, client):
        resp = client.post("/turns", json={
            "session_id": "test-sess",
            "user_id": "test-user",
            "messages": [],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 422

    def test_unicode_content(self, client):
        resp = client.post("/turns", json={
            "session_id": "unicode-sess",
            "user_id": None,
            "messages": [{"role": "user", "content": "你好！私は東京に住んでいます 🎌 café résumé naïve"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 201

    def test_very_long_content(self, client):
        long_msg = "x" * 10000
        resp = client.post("/turns", json={
            "session_id": "long-sess",
            "user_id": None,
            "messages": [{"role": "user", "content": long_msg}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 201

    def test_recall_with_no_data(self, client):
        resp = client.post("/recall", json={
            "query": "anything",
            "session_id": "nonexistent-session",
            "user_id": "nonexistent_user_xyz",
            "max_tokens": 512,
        })
        assert resp.status_code == 200
        assert resp.json()["context"] == ""

    def test_search_with_no_data(self, client):
        resp = client.post("/search", json={
            "query": "anything",
            "session_id": "nonexistent",
            "user_id": "nonexistent_user_xyz",
        })
        assert resp.status_code == 200
        assert resp.json()["results"] == []

    def test_delete_nonexistent_session(self, client):
        resp = client.delete("/sessions/does_not_exist_999")
        assert resp.status_code == 204

    def test_delete_nonexistent_user(self, client):
        resp = client.delete("/users/does_not_exist_999")
        assert resp.status_code == 204

    def test_special_chars_in_session_id(self, client):
        resp = client.post("/turns", json={
            "session_id": "sess-with-special_chars.123",
            "user_id": None,
            "messages": [{"role": "user", "content": "test"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 201
