"""Persistence test: data survives docker compose restart.

Must be run from the HOST machine (not from inside the container),
since it restarts the Docker Compose stack.

Usage:
    pytest tests/persistence/ -v
"""
import os
import subprocess
import time

import httpx
import pytest

BASE_URL = "http://localhost:8080"
PROJECT_DIR = os.path.join(os.path.dirname(__file__), "..", "..")


def _is_inside_docker():
    return os.path.exists("/.dockerenv")


@pytest.fixture(scope="module")
def client():
    return httpx.Client(base_url=BASE_URL, timeout=30.0)


def _wait_for_health(client, timeout=60):
    for _ in range(timeout // 2):
        try:
            r = client.get("/health")
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


@pytest.mark.skipif(_is_inside_docker(), reason="Must run from host — restarts containers")
def test_data_survives_restart(client):
    # Step 1: Write data
    client.delete("/users/persist-user")
    resp = client.post("/turns", json={
        "session_id": "persist-session",
        "user_id": "persist-user",
        "messages": [
            {"role": "user", "content": "I live in Paris and I work as a baker"},
            {"role": "assistant", "content": "Paris is beautiful!"},
        ],
        "timestamp": "2025-03-15T10:00:00Z",
    })
    assert resp.status_code == 201

    # Step 2: Restart containers
    subprocess.run(
        ["docker", "compose", "down"],
        check=True,
        capture_output=True,
        cwd=PROJECT_DIR,
    )
    subprocess.run(
        ["docker", "compose", "up", "-d"],
        check=True,
        capture_output=True,
        cwd=PROJECT_DIR,
    )

    assert _wait_for_health(client), "Service didn't come up after restart"

    # Step 3: Verify data survived
    resp = client.post("/recall", json={
        "query": "where does this user live",
        "session_id": "persist-session",
        "user_id": "persist-user",
        "max_tokens": 512,
    })
    assert resp.status_code == 200
    context = resp.json()["context"]
    assert "paris" in context.lower()

    resp = client.get("/users/persist-user/memories")
    assert resp.status_code == 200
    assert len(resp.json()["memories"]) > 0

    client.delete("/users/persist-user")
