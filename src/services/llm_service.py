import asyncio
import json
import logging

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


class LLMService:
    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=settings.OPENAI_BASE_URL or "https://api.openai.com/v1",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                timeout=60.0,
            )
        return self._client

    async def _post_with_retry(self, url: str, payload: dict) -> dict:
        for attempt in range(MAX_RETRIES):
            resp = await self.client.post(url, json=payload)
            if resp.status_code == 429 and attempt < MAX_RETRIES - 1:
                wait = min(2 ** (attempt + 1), 10)
                logger.warning(f"Rate limited, retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES})")
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()

    async def extract_memories(self, messages: list[dict]) -> list[dict]:
        from src.prompts.extract import EXTRACTION_SYSTEM_PROMPT, EXTRACTION_SCHEMA

        user_content = "\n".join(
            f"{m['role']}" + (f" ({m['name']})" if m.get('name') else "") + f": {m['content']}"
            for m in messages
        )

        data = await self._post_with_retry(
            "/chat/completions",
            {
                "model": settings.OPENAI_MODEL,
                "messages": [
                    {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "extraction",
                        "strict": True,
                        "schema": EXTRACTION_SCHEMA,
                    },
                },
                "temperature": 0.1,
            },
        )
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return parsed.get("memories", [])

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        data = await self._post_with_retry(
            "/embeddings",
            {
                "model": settings.EMBEDDING_MODEL,
                "input": texts,
                "dimensions": settings.EMBEDDING_DIMENSIONS,
            },
        )
        return [item["embedding"] for item in data["data"]]

    async def rerank(self, query: str, memories: list[dict]) -> list[int]:
        from src.prompts.rerank import RERANK_SYSTEM_PROMPT, RERANK_SCHEMA

        if not memories:
            return []

        numbered = "\n".join(
            f'{i+1}. "{m["value"]}" [{m["type"]}, key={m["key"]}]'
            for i, m in enumerate(memories)
        )

        user_content = (
            f'Query: "{query}"\n\n'
            f"Rank these memories by relevance to the query. "
            f"Return ONLY the indices in order of relevance.\n\n{numbered}"
        )

        data = await self._post_with_retry(
            "/chat/completions",
            {
                "model": settings.OPENAI_MODEL,
                "messages": [
                    {"role": "system", "content": RERANK_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "rerank",
                        "strict": True,
                        "schema": RERANK_SCHEMA,
                    },
                },
                "temperature": 0.1,
            },
        )
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        indices = parsed.get("ranked_indices", [])
        if indices and min(indices) == 0:
            return indices
        return [idx - 1 for idx in indices]

    async def check_contradiction(
        self, key: str, old_value: str, new_value: str
    ) -> dict:
        from src.prompts.contradiction import (
            CONTRADICTION_SCHEMA,
            CONTRADICTION_SYSTEM_PROMPT,
        )

        user_content = (
            f'Existing memory (key="{key}"): "{old_value}"\n'
            f'New extraction (key="{key}"): "{new_value}"'
        )

        data = await self._post_with_retry(
            "/chat/completions",
            {
                "model": settings.OPENAI_MODEL,
                "messages": [
                    {"role": "system", "content": CONTRADICTION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "contradiction",
                        "strict": True,
                        "schema": CONTRADICTION_SCHEMA,
                    },
                },
                "temperature": 0.1,
            },
        )
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


llm_service = LLMService()
