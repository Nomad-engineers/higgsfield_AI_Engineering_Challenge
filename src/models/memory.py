import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, Column, Computed, Float, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.turn import Base


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    key: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")
    source_session: Mapped[str] = mapped_column(String, nullable=False)
    source_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("turns.id", ondelete="SET NULL"), nullable=True
    )
    supersedes: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    embedding = mapped_column(Vector(1536), nullable=True)
    search_vector = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(key, '') || ' ' || coalesce(value, ''))", persisted=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"
    )

    __table_args__ = (
        Index("idx_memories_embedding", "embedding", postgresql_using="hnsw",
              postgresql_with={"m": 16, "ef_construction": 64},
              postgresql_ops={"embedding": "vector_cosine_ops"}),
        Index("idx_memories_user_active", "user_id", "active"),
        Index("idx_memories_key", "user_id", "key", postgresql_where="active = true"),
        Index("idx_memories_search", "search_vector", postgresql_using="gin"),
    )
