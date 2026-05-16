"""Persistence tests.

Two modes:
1. Inside Docker: verifies data is committed to PostgreSQL (not just in-memory)
   by writing via API and reading back through a fresh DB engine.
2. From host: full restart test — write, docker compose down/up, verify data
   survives via the named volume.
"""
import asyncio
import os
import subprocess
import time

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BASE_URL = "http://localhost:8080"
IS_DOCKER = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
HAS_DB_URL = os.environ.get("DATABASE_URL") is not None


@pytest.fixture(scope="module")
def client():
    return httpx.Client(base_url=BASE_URL, timeout=180.0)


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


# --- Inside-Docker test: DB-level persistence verification ---


@pytest.mark.skipif(not HAS_DB_URL, reason="DATABASE_URL not set — running outside app container")
def test_data_persisted_to_db(client):
    """Verify data written via API is committed to PostgreSQL."""
    user_id = "persist-db-test"
    session_id = "persist-db-session"

    client.delete(f"/users/{user_id}")
    resp = client.post("/turns", json={
        "session_id": session_id,
        "user_id": user_id,
        "messages": [
            {"role": "user", "content": "I live in Paris and I work as a baker"},
            {"role": "assistant", "content": "Paris is beautiful!"},
        ],
        "timestamp": "2025-03-15T10:00:00Z",
    })
    assert resp.status_code == 201

    # Verify recall works
    resp = client.post("/recall", json={
        "query": "where does this user live",
        "session_id": session_id,
        "user_id": user_id,
        "max_tokens": 512,
    })
    assert resp.status_code == 200
    assert "paris" in resp.json()["context"].lower()

    # Verify data in DB directly with a fresh engine
    async def _check_db():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        async with engine.begin() as conn:
            turns = await conn.execute(
                text("SELECT COUNT(*) FROM turns WHERE user_id = :uid"),
                {"uid": user_id},
            )
            assert turns.scalar() > 0, "No turns found in DB"

            memories = await conn.execute(
                text("SELECT COUNT(*) FROM memories WHERE user_id = :uid"),
                {"uid": user_id},
            )
            assert memories.scalar() > 0, "No memories found in DB"

            # Verify memory content is correct
            mem_rows = await conn.execute(
                text("SELECT key, value FROM memories WHERE user_id = :uid AND active = true"),
                {"uid": user_id},
            )
            mem_data = [(r[0], r[1]) for r in mem_rows.fetchall()]
            keys = [k for k, v in mem_data]
            assert "location" in keys, f"location not found in {keys}"

        await engine.dispose()

    asyncio.run(_check_db())

    client.delete(f"/users/{user_id}")


# --- Host-level test: full container restart ---


@pytest.mark.skipif(IS_DOCKER, reason="Cannot restart containers from inside Docker — run from host")
def test_data_survives_restart(client):
    """Write data, restart Docker Compose, verify data survives."""
    project_dir = os.path.join(os.path.dirname(__file__), "..", "..")
    user_id = "persist-restart-user"

    client.delete(f"/users/{user_id}")
    resp = client.post("/turns", json={
        "session_id": "persist-restart-session",
        "user_id": user_id,
        "messages": [
            {"role": "user", "content": "I live in Paris and I work as a baker"},
            {"role": "assistant", "content": "Paris is beautiful!"},
        ],
        "timestamp": "2025-03-15T10:00:00Z",
    })
    assert resp.status_code == 201

    subprocess.run(["docker", "compose", "down"], check=True, capture_output=True, cwd=project_dir)
    subprocess.run(["docker", "compose", "up", "-d"], check=True, capture_output=True, cwd=project_dir)
    assert _wait_for_health(client), "Service didn't come up after restart"

    resp = client.post("/recall", json={
        "query": "where does this user live",
        "session_id": "persist-restart-session",
        "user_id": user_id,
        "max_tokens": 512,
    })
    assert resp.status_code == 200
    assert "paris" in resp.json()["context"].lower()

    resp = client.get(f"/users/{user_id}/memories")
    assert resp.status_code == 200
    assert len(resp.json()["memories"]) > 0

    client.delete(f"/users/{user_id}")
