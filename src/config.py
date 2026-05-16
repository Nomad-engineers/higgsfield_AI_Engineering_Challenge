from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    OPENAI_API_KEY: str = ""
    MEMORY_AUTH_TOKEN: str = ""
    PORT: int = 8080
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/memory"
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_BASE_URL: str = ""
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSIONS: int = 1536
    LOG_LEVEL: str = "info"

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def llm_available(self) -> bool:
        return bool(self.OPENAI_API_KEY and self.OPENAI_API_KEY.startswith("sk-"))


settings = Settings()
