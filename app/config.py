from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://postgres:postgres@localhost:5432/newsgen"
    api_key: str = ""

    gnews_api_key: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    kbnews_base_url: str = "https://kbnews.onrender.com"
    kbnews_bot_email: str = ""
    kbnews_bot_password: str = ""

    @field_validator("database_url")
    @classmethod
    def use_asyncpg_driver(cls, value: str) -> str:
        """Accept plain postgres:// or postgresql:// URLs (as Supabase shows them)."""
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value[len(prefix):]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
