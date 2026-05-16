import logging

from sqlalchemy.ext.asyncio import AsyncSession

from src.repositories.memory_repo import MemoryRepo
from src.services.llm_service import llm_service

logger = logging.getLogger(__name__)


class ExtractionService:
    def __init__(self, session: AsyncSession):
        self.memory_repo = MemoryRepo(session)

    async def extract_and_store(
        self,
        messages: list[dict],
        user_id: str,
        session_id: str,
        turn_id=None,
    ) -> list[dict]:
        if not user_id:
            logger.info("Skipping extraction — no user_id")
            return []

        try:
            raw_memories = await llm_service.extract_memories(messages)
        except Exception as e:
            logger.warning(f"LLM extraction failed: {e}")
            return []

        if not raw_memories:
            logger.info("No memories extracted")
            return []

        stored = []
        to_embed = []

        for mem in raw_memories:
            memory = await self._resolve_memory(
                user_id=user_id,
                session_id=session_id,
                turn_id=turn_id,
                type_=mem["type"],
                key=mem["key"],
                value=mem["value"],
                confidence=mem.get("confidence", 1.0),
            )
            if memory:
                stored.append(memory)
                to_embed.append(memory)

        if to_embed:
            await self._batch_embed(to_embed)

        return [
            {"id": str(m.id), "type": m.type, "key": m.key, "value": m.value}
            for m in stored
        ]

    async def _resolve_memory(
        self,
        user_id: str,
        session_id: str,
        turn_id,
        type_: str,
        key: str,
        value: str,
        confidence: float,
    ) -> object | None:
        existing = await self.memory_repo.get_active_by_key(user_id, key)

        if not existing:
            memory = await self.memory_repo.create(
                user_id=user_id,
                type=type_,
                key=key,
                value=value,
                confidence=confidence,
                source_session=session_id,
                source_turn_id=turn_id,
            )
            logger.info(f"New memory: {key}={value[:80]}")
            return memory

        # Check relationship with each existing memory of same key
        best_relationship = "new"
        best_match = existing[0]

        for old_mem in existing:
            try:
                result = await llm_service.check_contradiction(
                    key=key, old_value=old_mem.value, new_value=value
                )
                relationship = result.get("relationship", "new")
                logger.info(
                    f"Contradiction check: {key} -> {relationship} "
                    f"({result.get('reason', '')[:60]})"
                )
                if relationship != "new":
                    best_relationship = relationship
                    best_match = old_mem
                    break
            except Exception as e:
                logger.warning(f"Contradiction check failed: {e}")
                continue

        if best_relationship == "new":
            memory = await self.memory_repo.create(
                user_id=user_id,
                type=type_,
                key=key,
                value=value,
                confidence=confidence,
                source_session=session_id,
                source_turn_id=turn_id,
            )
            logger.info(f"New memory (existing key but unrelated): {key}={value[:80]}")
            return memory

        if best_relationship == "nuance":
            memory = await self.memory_repo.create(
                user_id=user_id,
                type=type_,
                key=key,
                value=value,
                confidence=min(1.0, max(confidence, best_match.confidence) + 0.05),
                source_session=session_id,
                source_turn_id=turn_id,
                supersedes=best_match.id,
            )
            logger.info(f"Nuance added for {key}: {value[:80]}")
            return memory

        # update, contradiction, correction → deactivate only matched memory, supersede
        await self.memory_repo.deactivate_by_id(best_match.id)
        memory = await self.memory_repo.create(
            user_id=user_id,
            type=type_,
            key=key,
            value=value,
            confidence=confidence,
            source_session=session_id,
            source_turn_id=turn_id,
            supersedes=best_match.id,
        )
        logger.info(
            f"{best_relationship.title()}: {key} "
            f"'{best_match.value[:40]}' -> '{value[:40]}'"
        )
        return memory

    async def _batch_embed(self, memories: list):
        texts = [f"{m.key}: {m.value}" for m in memories]
        try:
            embeddings = await llm_service.embed(texts)
        except Exception as e:
            logger.warning(f"Embedding failed: {e}")
            return

        for memory, embedding in zip(memories, embeddings):
            memory.embedding = embedding
        await self.memory_repo.session.flush()
