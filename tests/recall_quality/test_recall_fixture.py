"""Recall quality test using scripted conversations + probe-based grading.

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

pytestmark = pytest.mark.integration

BASE_URL = "http://localhost:8080"
FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures")


@pytest.fixture(scope="module")
def client():
    return httpx.Client(base_url=BASE_URL, timeout=180.0)


@pytest.fixture(scope="module")
def conversations():
    with open(os.path.join(FIXTURES_DIR, "conversations.json")) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def probes():
    with open(os.path.join(FIXTURES_DIR, "probes.json")) as f:
        return json.load(f)["probes"]


def _cleanup(client, user_id):
    client.delete(f"/users/{user_id}")


@pytest.fixture(scope="module", autouse=True)
def seed_data(client, conversations):
    """Post all conversations before tests, clean up after."""
    for conv in conversations["conversations"]:
        _cleanup(client, conv["user_id"])

    for conv in conversations["conversations"]:
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

    for conv in conversations["conversations"]:
        _cleanup(client, conv["user_id"])


def _recall_contains(context: str, expected_facts: list[str]) -> tuple[int, int]:
    matched = 0
    for fact in expected_facts:
        if fact.lower() in context.lower():
            matched += 1
    return matched, len(expected_facts)


# --- Probe-based recall quality ---


def test_probe_recall_quality(client, probes):
    """Run all probes from probes.json and grade with must_match/must_not."""
    results = {"pass": 0, "fail": 0, "total": len(probes)}
    details = []

    for probe in probes:
        resp = client.post("/recall", json={
            "query": probe["query"],
            "session_id": f"probe-{probe['id']}",
            "user_id": probe["user_id"],
            "max_tokens": probe.get("max_tokens", 512),
        })
        assert resp.status_code == 200, f"Recall failed for probe {probe['id']}: {resp.text}"
        data = resp.json()
        context = data["context"].lower()

        if probe.get("expect_empty"):
            if not context:
                results["pass"] += 1
                details.append({"id": probe["id"], "status": "PASS", "reason": "empty as expected"})
            else:
                results["fail"] += 1
                details.append({"id": probe["id"], "status": "FAIL", "reason": f"expected empty, got {len(context)} chars"})
            time.sleep(1)
            continue

        must_match = [m.lower() for m in probe.get("must_match", [])]
        must_not = [m.lower() for m in probe.get("must_not", [])]

        matched = all(m in context for m in must_match)
        avoided = all(m not in context for m in must_not)

        if matched and avoided:
            results["pass"] += 1
            details.append({"id": probe["id"], "status": "PASS", "reason": ""})
        else:
            results["fail"] += 1
            reasons = []
            missing = [m for m in must_match if m not in context]
            leaked = [m for m in must_not if m in context]
            if missing:
                reasons.append(f"missing: {missing}")
            if leaked:
                reasons.append(f"leaked: {leaked}")
            details.append({"id": probe["id"], "status": "FAIL", "reason": "; ".join(reasons)})

        time.sleep(1)

    print("\n" + "=" * 60)
    print("PROBE-BASED RECALL QUALITY REPORT")
    print("=" * 60)
    for d in details:
        reason = f" — {d['reason']}" if d["reason"] else ""
        print(f"  [{d['status']}] {d['id']}: {reason}")
    print("-" * 60)
    pct = results["pass"] / results["total"] * 100 if results["total"] else 0
    print(f"  OVERALL: {results['pass']}/{results['total']} ({pct:.0f}%)")
    print("=" * 60)

    assert results["pass"] >= results["total"] * 0.6, (
        f"Too few probes passed: {results['pass']}/{results['total']}"
    )


# --- Legacy probe queries (from conversations.json) ---


def test_recall_queries(client, conversations):
    """Run all legacy probe queries and check recall quality."""
    results = []
    total_matched = 0
    total_expected = 0

    for probe in conversations["probe_queries"]:
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
    print("RECALL QUALITY REPORT (legacy probes)")
    print("=" * 60)
    for r in results:
        status = "PASS" if r["pct"] >= 70 else "FAIL"
        print(f"  [{status}] {r['query']} — {r['matched']}/{r['total']} ({r['pct']:.0f}%)")
    print("-" * 60)
    print(f"  OVERALL: {total_matched}/{total_expected} ({overall:.0f}%)")
    print("=" * 60)

    assert overall >= 70, f"Recall quality {overall:.0f}% is below 70% threshold"


# --- Specific behavior tests ---


def test_recall_returns_citations(client, conversations):
    """Verify recall responses include proper citations."""
    probe = conversations["probe_queries"][0]
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


def test_fact_evolution_employer_is_current(client):
    """Recall should return the LATEST employer (Stripe), not the old one (Notion)."""
    resp = client.post("/recall", json={
        "query": "What does this user do for work?",
        "session_id": "fact-evo-session",
        "user_id": "alice",
        "max_tokens": 512,
    })
    assert resp.status_code == 200
    context = resp.json()["context"].lower()

    assert "stripe" in context, "Expected current employer 'Stripe' in recall context"
    assert "notion" not in context, "Old employer 'Notion' should NOT appear — superseded by Stripe"


def test_fact_evolution_history_preserved(client):
    """The /memories endpoint should show the superseded Notion memory with correct metadata."""
    resp = client.get("/users/alice/memories")
    assert resp.status_code == 200
    memories = resp.json()["memories"]

    notion_mem = None
    stripe_mem = None
    for m in memories:
        val = m["value"].lower()
        if "notion" in val and m["key"] == "employer":
            notion_mem = m
        if "stripe" in val and m["key"] == "employer":
            stripe_mem = m

    assert stripe_mem is not None, "Expected a Stripe employer memory to exist"
    assert stripe_mem["active"] is True, "Stripe memory should be active"

    assert notion_mem is not None, "Expected a Notion employer memory to exist"
    assert notion_mem["active"] is False, "Notion memory should be inactive — superseded by Stripe"
    assert notion_mem["superseded_by"] is not None, "Notion memory should have superseded_by set"
    assert notion_mem["superseded_by"] == stripe_mem["id"], "Notion's superseded_by should point to the Stripe memory"


def test_noise_resistance_unrelated_query(client):
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
    assert not leaked, f"Noise leak: unrelated facts {leaked} found in context for car query"


def test_noise_resistance_context_is_minimal_for_unrelated(client):
    """An unrelated query should return empty or very short context."""
    resp = client.post("/recall", json={
        "query": "quantum physics equations",
        "session_id": "noise-physics-session",
        "user_id": "alice",
        "max_tokens": 512,
    })
    assert resp.status_code == 200
    body = resp.json()
    context = body["context"]

    if context:
        assert len(context) < 300, (
            f"Noise: unrelated query returned {len(context)} chars of context, "
            f"expected < 300. Context: {context[:200]}"
        )
