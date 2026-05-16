"""Shared test configuration.

Modes:
1. Docker integration tests — use httpx.Client against localhost:8080
2. Hermetic tests — pure Python, no external dependencies (tests/hermetic/)

Running without Docker:
  pytest tests/hermetic/ -v         # 36 tests, zero deps
  pytest -v                          # skips integration tests automatically
Running with Docker:
  pytest -v                          # runs all tests including integration
"""
import os
import socket

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/memory")


def _server_reachable(host="localhost", port=8080, timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: requires Docker stack")
    config.addinivalue_line("markers", "hermetic: runs without external dependencies")


def pytest_collection_modifyitems(config, items):
    if _server_reachable():
        return
    skip = pytest.mark.skip(reason="Docker stack not running (localhost:8080 unreachable)")
    for item in items:
        if "integration" in [m.name for m in item.iter_markers()]:
            item.add_marker(skip)
