"""Application configuration — loads from environment variables."""

import os
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Authentication
    team_password_hash: str = ""
    jwt_secret: str = "changeme-set-a-real-secret-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expiry_days: int = 30

    # Database
    database_url: str = ""  # Empty = use SQLite

    # AI
    anthropic_api_key: str = ""

    # Server
    port: int = 8000

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False

    @property
    def effective_database_url(self) -> str:
        """Return the database URL, falling back to SQLite."""
        if self.database_url:
            return self.database_url
        return "sqlite+aiosqlite:///./business_analyst.db"

    @property
    def is_sqlite(self) -> bool:
        """True when using the SQLite fallback."""
        return not bool(self.database_url)


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
