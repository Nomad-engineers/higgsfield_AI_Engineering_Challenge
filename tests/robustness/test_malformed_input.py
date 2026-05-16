"""Robustness tests: malformed input, unicode, edge cases, injection."""
import httpx
import pytest

pytestmark = pytest.mark.integration

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

    def test_null_session_id(self, client):
        resp = client.post("/turns", json={
            "session_id": None,
            "messages": [{"role": "user", "content": "hi"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 422

    def test_null_timestamp(self, client):
        resp = client.post("/turns", json={
            "session_id": "null-ts-sess",
            "messages": [{"role": "user", "content": "hi"}],
            "timestamp": None,
        })
        assert resp.status_code == 422

    def test_null_content_in_message(self, client):
        resp = client.post("/turns", json={
            "session_id": "null-content-sess",
            "messages": [{"role": "user", "content": None}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 422

    def test_oversized_payload(self, client):
        big_content = "x" * 100000
        resp = client.post("/turns", json={
            "session_id": "oversized-sess",
            "user_id": None,
            "messages": [{"role": "user", "content": big_content}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 201


class TestUnicode:
    def test_cjk_content(self, client):
        resp = client.post("/turns", json={
            "session_id": "cjk-sess",
            "user_id": None,
            "messages": [{"role": "user", "content": "你好世界"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 201

    def test_emoji_content(self, client):
        resp = client.post("/turns", json={
            "session_id": "emoji-sess",
            "user_id": None,
            "messages": [{"role": "user", "content": "Hello 🎌 🚀 🎉"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 201

    def test_mixed_unicode(self, client):
        resp = client.post("/turns", json={
            "session_id": "unicode-sess",
            "user_id": None,
            "messages": [{"role": "user", "content": "你好！私は東京に住んでいます 🎌 café résumé naïve"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 201

    def test_unicode_in_query(self, client):
        resp = client.post("/recall", json={
            "query": "用户住在哪里？",
            "session_id": "unicode-query-sess",
            "max_tokens": 256,
        })
        assert resp.status_code == 200


class TestInjection:
    def test_sql_injection_in_content(self, client):
        resp = client.post("/turns", json={
            "session_id": "sql-inject-sess",
            "user_id": None,
            "messages": [{"role": "user", "content": "'; DROP TABLE memories; --"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 201

    def test_sql_injection_in_session_id(self, client):
        resp = client.post("/turns", json={
            "session_id": "'; DROP TABLE turns; --",
            "user_id": None,
            "messages": [{"role": "user", "content": "test"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code in (201, 422)

    def test_sql_injection_in_recall_query(self, client):
        resp = client.post("/recall", json={
            "query": "'; SELECT * FROM memories WHERE '1'='1",
            "session_id": "sql-inject-recall",
            "max_tokens": 256,
        })
        assert resp.status_code == 200

    def test_sql_injection_in_search_query(self, client):
        resp = client.post("/search", json={
            "query": "UNION SELECT * FROM memories --",
            "user_id": "inject-user",
        })
        assert resp.status_code == 200

    def test_html_in_content(self, client):
        resp = client.post("/turns", json={
            "session_id": "html-sess",
            "user_id": None,
            "messages": [{"role": "user", "content": "<script>alert('xss')</script>"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 201


class TestEdgeCases:
    def test_very_long_session_id(self, client):
        long_id = "sess-" + "x" * 500
        resp = client.post("/turns", json={
            "session_id": long_id,
            "user_id": None,
            "messages": [{"role": "user", "content": "test"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 201

    def test_special_chars_in_session_id(self, client):
        resp = client.post("/turns", json={
            "session_id": "sess-with-special_chars.123",
            "user_id": None,
            "messages": [{"role": "user", "content": "test"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 201

    def test_very_long_query(self, client):
        long_query = "word " * 5000
        resp = client.post("/recall", json={
            "query": long_query,
            "session_id": "long-query-sess",
            "max_tokens": 128,
        })
        assert resp.status_code == 200

    def test_negative_max_tokens(self, client):
        resp = client.post("/recall", json={
            "query": "test",
            "session_id": "neg-tokens-sess",
            "max_tokens": -1,
        })
        assert resp.status_code in (200, 422)

    def test_zero_max_tokens(self, client):
        resp = client.post("/recall", json={
            "query": "test",
            "session_id": "zero-tokens-sess",
            "max_tokens": 0,
        })
        assert resp.status_code in (200, 422)

    def test_message_with_name_field(self, client):
        resp = client.post("/turns", json={
            "session_id": "name-field-sess",
            "user_id": None,
            "messages": [{"role": "user", "content": "hi", "name": "Alice"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        assert resp.status_code == 201


class TestEmptyData:
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

    def test_memories_for_nonexistent_user(self, client):
        resp = client.get("/users/absolutely_nobody_12345/memories")
        assert resp.status_code == 200
        assert resp.json()["memories"] == []
