"""Concurrent sessions test: no cross-user data bleeding."""
import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = "http://localhost:8080"


@pytest.fixture(scope="module")
def client():
    return httpx.Client(base_url=BASE_URL, timeout=30.0)


@pytest.fixture(autouse=True)
def cleanup(client):
    yield
    client.delete("/users/concurrent-user-a")
    client.delete("/users/concurrent-user-b")
    client.delete("/users/concurrent-user-c")
    client.delete("/sessions/concurrent-sess-a")
    client.delete("/sessions/concurrent-sess-b")
    client.delete("/sessions/concurrent-sess-c")
    client.delete("/sessions/concurrent-sess-a2")


class TestCrossUserIsolation:
    def test_no_cross_user_bleed(self, client):
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

    def test_memories_separate_per_user(self, client):
        # Post data for both users
        client.post("/turns", json={
            "session_id": "concurrent-sess-a",
            "user_id": "concurrent-user-a",
            "messages": [{"role": "user", "content": "I live in Tokyo"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        client.post("/turns", json={
            "session_id": "concurrent-sess-b",
            "user_id": "concurrent-user-b",
            "messages": [{"role": "user", "content": "I live in London"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })

        resp_a = client.get("/users/concurrent-user-a/memories")
        resp_b = client.get("/users/concurrent-user-b/memories")
        assert resp_a.status_code == 200
        assert resp_b.status_code == 200
        assert len(resp_a.json()["memories"]) > 0
        assert len(resp_b.json()["memories"]) > 0

        # Verify no cross-contamination in memory values
        a_values = " ".join(m["value"].lower() for m in resp_a.json()["memories"])
        b_values = " ".join(m["value"].lower() for m in resp_b.json()["memories"])
        assert "tokyo" in a_values
        assert "london" not in a_values
        assert "london" in b_values
        assert "tokyo" not in b_values


class TestMultiSessionSameUser:
    def test_multi_session_recall(self, client):
        """Same user, different sessions — recall should aggregate across sessions."""
        client.post("/turns", json={
            "session_id": "concurrent-sess-a",
            "user_id": "concurrent-user-a",
            "messages": [{"role": "user", "content": "I live in Tokyo"}],
            "timestamp": "2025-03-15T10:00:00Z",
        })
        client.post("/turns", json={
            "session_id": "concurrent-sess-a2",
            "user_id": "concurrent-user-a",
            "messages": [{"role": "user", "content": "I work at Sony"}],
            "timestamp": "2025-03-15T11:00:00Z",
        })

        resp = client.post("/recall", json={
            "query": "tell me about this user",
            "session_id": "concurrent-sess-a-recall",
            "user_id": "concurrent-user-a",
            "max_tokens": 512,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body["context"], str)
        assert isinstance(body["citations"], list)


class TestThreeUserIsolation:
    def test_three_users_no_bleed(self, client):
        """Three users with different data — no cross-contamination."""
        users = [
            ("concurrent-user-a", "concurrent-sess-a", "I live in Tokyo"),
            ("concurrent-user-b", "concurrent-sess-b", "I live in London"),
            ("concurrent-user-c", "concurrent-sess-c", "I live in Berlin"),
        ]

        for uid, sid, msg in users:
            resp = client.post("/turns", json={
                "session_id": sid,
                "user_id": uid,
                "messages": [{"role": "user", "content": msg}],
                "timestamp": "2025-03-15T10:00:00Z",
            })
            assert resp.status_code == 201

        cities = {"concurrent-user-a": "tokyo", "concurrent-user-b": "london", "concurrent-user-c": "berlin"}
        all_cities = set(cities.values())

        for uid, city in cities.items():
            resp = client.post("/recall", json={
                "query": "where does the user live",
                "session_id": f"recall-{uid}",
                "user_id": uid,
                "max_tokens": 512,
            })
            assert resp.status_code == 200
            context = resp.json()["context"].lower()
            assert city in context, f"Expected '{city}' in context for {uid}, got: {context}"
            other_cities = all_cities - {city}
            for other in other_cities:
                assert other not in context, f"Unexpected '{other}' in context for {uid}"
