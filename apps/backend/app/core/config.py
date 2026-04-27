"""Application configuration."""

from pathlib import Path
from urllib.parse import parse_qs, urlencode

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


def find_env_file() -> Path | None:
    """Find .env file in multiple possible locations."""
    import os
    current_dir = os.path.dirname(os.path.abspath(__file__))

    possible_paths = [
        os.path.join(current_dir, ".env"),
        os.path.join(os.path.dirname(current_dir), ".env"),
        os.path.join(os.path.dirname(os.path.dirname(current_dir)), ".env"),
    ]

    for path_str in possible_paths:
        if os.path.exists(path_str):
            path = Path(path_str)
            LOGGER.info(f"Found .env file at: {path}")
            return path

    LOGGER.warning("No .env file found in expected locations")
    return None


ENV_FILE = find_env_file()


class DatabaseSettings(BaseSettings):
    """Database connection and pool settings."""

    url: str = Field(
        default="postgresql+asyncpg://converio:converio@localhost:5432/converio",
        validation_alias="DATABASE_URL",
    )
    postgres_url: str | None = Field(default=None, validation_alias="POSTGRES_URL")
    postgres_url_non_pooling: str | None = Field(
        default=None, validation_alias="POSTGRES_URL_NON_POOLING"
    )

    pool_size: int = Field(default=10, validation_alias="DATABASE_POOL_SIZE")
    use_local_db: bool = Field(default=True, validation_alias="USE_LOCAL_DB")

    @property
    def connection_url(self) -> str:
        """Get the connection URL with the correct asyncpg prefix."""
        if self.use_local_db:
            raw_url = self.url
        else:
            raw_url = self.postgres_url or self.postgres_url_non_pooling or self.url

        if not raw_url:
            return ""

        if "://" in raw_url:
            scheme, rest = raw_url.split("://", 1)
            base_scheme = "postgresql+asyncpg"

            if "?" in rest:
                path, query = rest.split("?", 1)
                params = parse_qs(query)

                if "sslmode" in params:
                    params["ssl"] = params.pop("sslmode")

                if "supa" in params:
                    params.pop("supa")

                params["statement_cache_size"] = ["0"]
                new_query = urlencode(params, doseq=True)
                return f"{base_scheme}://{path}?{new_query}"
            else:
                return f"{base_scheme}://{rest}?statement_cache_size=0"

        return raw_url

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE) if ENV_FILE else None,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


class LLMSettings(BaseSettings):
    """LLM provider settings."""

    provider: str = Field(default="ollama", validation_alias="LLM_PROVIDER")

    # OpenRouter
    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    openrouter_api_url: str = Field(
        default="https://openrouter.ai/api/v1",
        validation_alias="OPENROUTER_API_URL",
    )
    openrouter_model: str = Field(
        default="google/gemini-2.0-flash-001",
        validation_alias="OPENROUTER_MODEL",
    )

    # Gemini direct
    gemini_api_key: str = Field(default="", validation_alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.0-flash", validation_alias="GEMINI_MODEL")

    # Ollama (local — default for dev, zero token cost)
    ollama_host: str = Field(default="http://localhost:11434", validation_alias="OLLAMA_HOST")
    ollama_model: str = Field(default="qwen2.5:7b", validation_alias="OLLAMA_MODEL")

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE) if ENV_FILE else None,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


class TemporalSettings(BaseSettings):
    """Temporal connection and workflow settings."""

    host: str = Field(default="localhost", validation_alias="TEMPORAL_HOST")
    port: int = Field(default=7233, validation_alias="TEMPORAL_PORT")
    namespace: str = Field(default="default", validation_alias="TEMPORAL_NAMESPACE")
    task_queue: str = Field(default="converio-queue", validation_alias="TEMPORAL_TASK_QUEUE")

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE) if ENV_FILE else None,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


class Neo4jSettings(BaseSettings):
    """Neo4j connection and graph settings."""

    host: str = Field(default="localhost", validation_alias="NEO4J_HOST")
    port: int = Field(default=7687, validation_alias="NEO4J_PORT")
    username: str = Field(default="neo4j", validation_alias="NEO4J_USERNAME")
    password: str = Field(default="", validation_alias="NEO4J_PASSWORD")
    use_local_neo4j: bool = Field(default=True, validation_alias="USE_LOCAL_NEO4J")

    @property
    def uri(self) -> str:
        """Get the connection URI with the correct protocol."""
        if "://" in self.host:
            return self.host
        protocol = "neo4j" if self.use_local_neo4j else "neo4j+s"
        return f"{protocol}://{self.host}:{self.port}"

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE) if ENV_FILE else None,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


class SupabaseSettings(BaseSettings):
    """Supabase authentication settings."""

    url: str = Field(default="", validation_alias="SUPABASE_URL")
    anon_key: str = Field(default="", validation_alias="SUPABASE_ANON_KEY")
    service_role_key: str = Field(default="", validation_alias="SUPABASE_SERVICE_ROLE_KEY")
    storage_bucket: str = Field(default="resumes", validation_alias="SUPABASE_STORAGE_BUCKET")
    jwt_secret: str = Field(default="", validation_alias="SUPABASE_JWT_SECRET")
    jwks_cache_ttl: int = Field(default=3600, validation_alias="SUPABASE_JWKS_CACHE_TTL")

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE) if ENV_FILE else None,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


class Settings(BaseSettings):
    """Unified application settings with nested models."""

    app_name: str = Field(default="Converio", validation_alias="APP_NAME")
    app_version: str = "0.1.0"
    environment: str = Field(default="development", validation_alias="ENVIRONMENT")
    debug: bool = Field(default=True, validation_alias="DEBUG")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    cors_origins: list[str] = Field(
        default=["http://localhost:3000", "http://127.0.0.1:3000"],
        validation_alias="CORS_ORIGINS",
    )

    api_v1_prefix: str = "/api/v1"
    host: str = Field(default="0.0.0.0", validation_alias="HOST")
    port: int = Field(default=8000, validation_alias="PORT")

    db_init_timeout: int = 30

    db: DatabaseSettings = Field(default_factory=lambda: DatabaseSettings())
    llm: LLMSettings = Field(default_factory=lambda: LLMSettings())
    temporal: TemporalSettings = Field(default_factory=lambda: TemporalSettings())
    neo4j: Neo4jSettings = Field(default_factory=lambda: Neo4jSettings())
    supabase: SupabaseSettings = Field(default_factory=lambda: SupabaseSettings())

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE) if ENV_FILE else None,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def database_url(self) -> str:
        return self.db.connection_url

    @property
    def supabase_url(self) -> str:
        return self.supabase.url

    @property
    def supabase_jwt_secret(self) -> str:
        return self.supabase.jwt_secret

    @property
    def supabase_jwks_cache_ttl(self) -> int:
        return self.supabase.jwks_cache_ttl


# Initialize settings
settings = Settings()

LOGGER.info(f"Settings initialized with environment: {settings.environment}")
LOGGER.info(f"Database settings: local={settings.db.use_local_db}, pool={settings.db.pool_size}")
LOGGER.info(f"Neo4j settings: local={settings.neo4j.use_local_neo4j}, host={settings.neo4j.host}")
