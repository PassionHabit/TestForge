from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TESTFORGE_", env_file=".env")

    database_url: str = "sqlite:///./testforge.db"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    lease_seconds: int = 60
    agent_offline_seconds: int = 90
    log_chunk_max_bytes: int = 256 * 1024
    artifact_root: str = "./work/artifacts"


@lru_cache
def get_settings() -> Settings:
    return Settings()
