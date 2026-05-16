"""Recall quality test using scripted conversations.

Runs against the live Docker Compose stack with a real OpenAI API key.
Usage:
  OPENAI_API_KEY=sk-... docker compose up -d
  docker compose exec app python -m pytest tests/recall_quality/ -v
"""
import json
import os
import time

import httpx
import pytest

BASE_URL = "http://localhost:8080"
FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures")


@pytest.fixture(scope="module")
def client():
    return httpx.Client(base_url=BASE_URL, timeout=180.0)


@pytest.fixture(scope="module")
def fixtures():
    with open(os.path.join(FIXTURES_DIR, "conversations.json")) as f:
        return json.load(f)


def _cleanup(client, user_id):
    client.delete(f"/users/{user_id}")


@pytest.fixture(scope="module", autouse=True)
def seed_data(client, fixtures):
    """Post all conversations before tests, clean up after."""
    for conv in fixtures["conversations"]:
        _cleanup(client, conv["user_id"])

    for conv in fixtures["conversations"]:
        for turn in conv["turns"]:
            resp = client.post("/turns", json={
                "session_id": conv["session_id"],
                "user_id": conv["user_id"],
                "messages": turn["messages"],
                "timestamp": turn["timestamp"],
            })
            assert resp.status_code == 201, f"Failed to post turn: {resp.text}"
            time.sleep(3)

    yield

    for conv in fixtures["conversations"]:
        _cleanup(client, conv["user_id"])


def _recall_contains(context: str, expected_facts: list[str]) -> tuple[int, int]:
    """Return (matched, total) for expected facts in recall context."""
    matched = 0
    for fact in expected_facts:
        if fact.lower() in context.lower():
            matched += 1
    return matched, len(expected_facts)


def test_recall_queries(client, fixtures):
    """Run all probe queries and check recall quality."""
    results = []
    total_matched = 0
    total_expected = 0

    for probe in fixtures["probe_queries"]:
        resp = client.post("/recall", json={
            "query": probe["query"],
            "session_id": probe.get("session_id", "probe-session"),
            "user_id": probe["user_id"],
            "max_tokens": probe.get("max_tokens", 512),
        })
        assert resp.status_code == 200, f"Recall failed: {resp.text}"
        body = resp.json()
        context = body["context"]

        matched, total = _recall_contains(context, probe["expected_facts"])
        total_matched += matched
        total_expected += total

        pct = (matched / total * 100) if total > 0 else 0
        time.sleep(2)
        results.append({
            "query": probe["query"],
            "user_id": probe["user_id"],
            "matched": matched,
            "total": total,
            "pct": pct,
            "context_len": len(context),
        })

    overall = (total_matched / total_expected * 100) if total_expected > 0 else 0

    print("\n" + "=" * 60)
    print("RECALL QUALITY REPORT")
    print("=" * 60)
    for r in results:
        status = "PASS" if r["pct"] >= 70 else "FAIL"
        print(f"  [{status}] {r['query']} — {r['matched']}/{r['total']} ({r['pct']:.0f}%)")
    print("-" * 60)
    print(f"  OVERALL: {total_matched}/{total_expected} ({overall:.0f}%)")
    print("=" * 60)

    assert overall >= 70, f"Recall quality {overall:.0f}% is below 70% threshold"


def test_recall_returns_citations(client, fixtures):
    """Verify recall responses include proper citations."""
    probe = fixtures["probe_queries"][0]
    resp = client.post("/recall", json={
        "query": probe["query"],
        "session_id": probe.get("session_id", "probe-session"),
        "user_id": probe["user_id"],
        "max_tokens": probe.get("max_tokens", 512),
    })
    body = resp.json()
    assert "citations" in body
    if body["context"]:
        assert len(body["citations"]) > 0


def test_recall_empty_for_unknown_user(client):
    """Recall for unknown user returns empty context."""
    resp = client.post("/recall", json={
        "query": "anything",
        "session_id": "nonexistent-session",
        "user_id": "nonexistent_user_999",
        "max_tokens": 512,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["context"] == ""
    assert body["citations"] == []


def test_noise_resistance_unrelated_query(client, fixtures):
    """Unrelated query should not dump entire user profile."""
    resp = client.post("/recall", json={
        "query": "What car does the user drive?",
        "session_id": "noise-test-session",
        "user_id": "alice",
        "max_tokens": 512,
    })
    assert resp.status_code == 200
    body = resp.json()
    context = body["context"].lower()

    unrelated_facts = ["shellfish", "carlos", "japan", "biscuit", "golden retriever"]
    leaked = [f for f in unrelated_facts if f in context]
    assert not leaked, (
        f"Noise leak: unrelated facts {leaked} found in context for car query"
    )
