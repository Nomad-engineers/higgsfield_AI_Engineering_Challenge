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


from src.routers import health  # noqa: E402

app.include_router(health.router)
