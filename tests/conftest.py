"""Shared test configuration.

Two modes:
1. Docker integration tests — use httpx.Client against localhost:8080
2. Hermetic tests — use httpx.AsyncClient with ASGI transport (requires DB)

The hermetic async_client fixture is lazy-loaded to avoid import errors
when pytest_asyncio is not installed (e.g. in minimal environments).
"""
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/memory")


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "integration: requires Docker stack")
