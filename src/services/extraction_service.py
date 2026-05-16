import logging
from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession

from src.repositories.memory_repo import MemoryRepo
from src.services.llm_service import llm_service
from src.services.rule_extractor import RuleExtractor, normalize_key
from src.config import settings

logger = logging.getLogger(__name__)

TYPE_PRIORITY = {"fact": 0, "preference": 1, "event": 2, "opinion": 3}

CROSS_KEY_SIMILARITY_THRESHOLD = 0.70
CROSS_KEY_CHECK_TYPES = {"fact", "preference"}

ALWAYS_CROSS_CHECK_PAIRS = {
    frozenset({"employer", "title"}),
    frozenset({"employer", "occupation"}),
    frozenset({"location", "city"}),
    frozenset({"spouse", "spouse_occupation"}),
}


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

        # Rules always run
        rule_memories = RuleExtractor().extract(messages)

        # LLM only if key is available
        llm_memories = []
        if settings.llm_available:
            try:
                llm_memories = await llm_service.extract_memories(messages)
            except Exception as e:
                logger.warning(f"LLM extraction failed: {e}")

        raw_memories = self._merge_extractions(rule_memories, llm_memories)

        if not raw_memories:
            logger.info("No memories extracted")
            return []

        raw_memories = self._dedup_same_turn(raw_memories)

        stored = []
        to_embed = []

        for mem in raw_memories:
            mem["confidence"] = max(0.0, min(1.0, mem.get("confidence", 1.0)))
            memory = await self._resolve_memory(
                user_id=user_id,
                session_id=session_id,
                turn_id=turn_id,
                type_=mem["type"],
                key=mem["key"],
                value=mem["value"],
                confidence=mem["confidence"],
            )
            if memory:
                stored.append(memory)
                to_embed.append(memory)

        if to_embed:
            await self._batch_embed(to_embed)

        re_embed_needed = await self._cross_key_contradiction_check(
            user_id, stored,
        )

        if re_embed_needed:
            await self._batch_embed(re_embed_needed)

        return [
            {"id": str(m.id), "type": m.type, "key": m.key, "value": m.value}
            for m in stored
        ]

    def _merge_extractions(self, rules: list[dict], llm: list[dict]) -> list[dict]:
        """LLM wins on key conflict (richer values), rules fill gaps."""
        merged = {}

        for r in rules:
            key = normalize_key(r["key"])
            merged[key] = {**r, "key": key}

        for r in llm:
            key = normalize_key(r.get("key", ""))
            if not key:
                continue
            merged[key] = {**r, "key": key}

        return list(merged.values())

    def _dedup_same_turn(self, raw_memories: list[dict]) -> list[dict]:
        grouped = defaultdict(list)
        for mem in raw_memories:
            grouped[mem["key"]].append(mem)

        result = []
        for key, group in grouped.items():
            if len(group) == 1:
                result.append(group[0])
                continue

            group.sort(key=lambda m: (TYPE_PRIORITY.get(m["type"], 4), -len(m["value"])))

            merged = {
                **group[0],
                "confidence": max(m.get("confidence", 1.0) for m in group),
            }
            logger.info(
                f"Same-turn dedup: merged {len(group)} memories for key '{key}'"
            )
            result.append(merged)

        return result

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
        existing = await self.memory_repo.get_active_by_key(user_id, key, for_update=False)

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

        old_mem = existing[0]
        best_relationship = "new"
        best_match = old_mem

        try:
            result = await llm_service.check_contradiction(
                key=key, old_value=old_mem.value, new_value=value
            )
            best_relationship = result.get("relationship", "new")
            logger.info(
                f"Contradiction check: {key} -> {best_relationship} "
                f"({result.get('reason', '')[:60]})"
            )
        except Exception as e:
            logger.warning(f"Contradiction check failed: {e}")

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
            logger.info(f"Nuance added for {key}: {value[:80]} (old kept active)")
            return memory

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

    async def _cross_key_contradiction_check(
        self, user_id: str, new_memories: list,
    ) -> list:
        if not new_memories:
            return []

        candidates = [
            m for m in new_memories
            if m.type in CROSS_KEY_CHECK_TYPES and m.embedding
        ]
        if not candidates:
            return []

        re_embed_needed = []

        for new_mem in candidates:
            similar = await self.memory_repo.find_cross_key_similar(
                user_id=user_id,
                embedding=new_mem.embedding,
                exclude_key=new_mem.key,
                exclude_id=new_mem.id,
                threshold=CROSS_KEY_SIMILARITY_THRESHOLD,
                limit=5,
            )

            similar_keys = {old_mem.key for old_mem, _ in similar}
            for pair in ALWAYS_CROSS_CHECK_PAIRS:
                if new_mem.key in pair:
                    paired_key = next(iter(pair - {new_mem.key}))
                    if paired_key not in similar_keys:
                        paired_memories = await self.memory_repo.get_active_by_key(
                            user_id, paired_key
                        )
                        for pm in paired_memories:
                            similar.append((pm, 0.0))

            for old_mem, similarity in similar:
                try:
                    result = await llm_service.check_cross_key_contradiction(
                        new_key=new_mem.key,
                        new_value=new_mem.value,
                        new_type=new_mem.type,
                        old_key=old_mem.key,
                        old_value=old_mem.value,
                        old_type=old_mem.type,
                    )
                except Exception as e:
                    logger.warning(f"Cross-key contradiction check failed: {e}")
                    continue

                action = result.get("action", "keep_both")
                relationship = result.get("relationship", "independent")
                logger.info(
                    f"Cross-key check: {new_mem.key} vs {old_mem.key} "
                    f"(sim={similarity:.3f}) -> {relationship}/{action} "
                    f"({result.get('reason', '')[:60]})"
                )

                if action == "supersede_old":
                    await self.memory_repo.deactivate_by_id(old_mem.id)
                    logger.info(
                        f"Cross-key supersede: deactivated {old_mem.key} "
                        f"('{old_mem.value[:40]}') due to {new_mem.key}"
                    )
                elif action == "merge":
                    await self.memory_repo.deactivate_by_id(old_mem.id)
                    new_mem.value = f"{new_mem.value}; previously {old_mem.value}"
                    re_embed_needed.append(new_mem)
                    logger.info(
                        f"Cross-key merge: {new_mem.key} absorbed {old_mem.key}"
                    )

        return re_embed_needed

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
