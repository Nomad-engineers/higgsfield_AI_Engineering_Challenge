from sqlalchemy.ext.asyncio import AsyncSession

from src.repositories.turn_repo import TurnRepo


class RecallService:
    def __init__(self, session: AsyncSession):
        self.turn_repo = TurnRepo(session)

    async def recall(self, query: str, user_id: str, session_id: str | None = None,
                     max_tokens: int = 512) -> tuple[str, list[dict]]:
        turns = await self.turn_repo.get_recent_by_user(user_id, limit=3)
        if not turns:
            return "", []

        lines = []
        citations = []
        for turn in reversed(turns):
            for msg in turn.messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                lines.append(f"{role}: {content}")
            snippet = str(turn.messages[-1].get("content", ""))[:100] if turn.messages else ""
            citations.append({
                "memory_id": str(turn.id),
                "turn_id": str(turn.id),
                "score": 1.0,
                "snippet": snippet,
            })

        context = "\n".join(lines)
        max_chars = max_tokens * 4
        if len(context) > max_chars:
            context = context[:max_chars]

        return context, citations
