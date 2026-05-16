import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.config import settings

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper()))
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _init_schema()
    logger.info("Memory service started")
    yield
    logger.info("Memory service shutting down")


async def _init_schema():
    from src.database import engine

    async with engine.begin() as conn:
        await conn.execute(
            __import__("sqlalchemy").text("CREATE EXTENSION IF NOT EXISTS vector")
        )
        await conn.run_sync(_create_tables)

    logger.info("Database schema initialized")


def _create_tables(sync_conn):
    from src.models.memory import Memory
    from src.models.turn import Base, Turn

    Base.metadata.create_all(sync_conn)


app = FastAPI(title="Memory Service", version="0.1.0", lifespan=lifespan)

# Middleware
from src.middleware.auth import AuthMiddleware
from src.middleware.error_handler import ErrorHandlerMiddleware

app.add_middleware(ErrorHandlerMiddleware)
if settings.MEMORY_AUTH_TOKEN:
    app.add_middleware(AuthMiddleware, token=settings.MEMORY_AUTH_TOKEN)

# Routers
from src.routers import cleanup, health, memories, recall, search, turns  # noqa: E402

app.include_router(health.router)
app.include_router(turns.router)
app.include_router(recall.router)
app.include_router(search.router)
app.include_router(memories.router)
app.include_router(cleanup.router)
