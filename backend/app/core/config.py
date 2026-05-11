from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "SlideMind"
    api_prefix: str = "/api"
    debug: bool = True
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "slidemind"

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "slidemind_password"

    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_collection: str = "landslide_knowledge_vectors"
    embedding_dim: int = 384

    redis_url: str = "redis://localhost:6379/0"
    upload_dir: Path = Path("uploads")
    async_imports: bool = False
    db_init_retries: int = 12
    db_init_retry_seconds: float = 2.0

    llm_provider: str = "mock"
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_model: str = "gpt-4.1-mini"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    return settings


settings = get_settings()
