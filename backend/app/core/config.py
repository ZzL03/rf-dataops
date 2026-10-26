from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    """Load application settings; credentials never appear in the URL repr."""

    model_config = SettingsConfigDict(
        env_prefix="RF_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    database_host: str = Field(default="127.0.0.1", min_length=1)
    database_port: int = Field(default=55432, ge=1, le=65535)
    database_user: str = Field(default="rf_dataops", min_length=1)
    database_password: SecretStr = Field(min_length=1)
    database_name: str = Field(default="rf_dataops", min_length=1)
    database_connect_timeout: int = Field(default=3, ge=1, le=30)
    database_statement_timeout_ms: int = Field(default=3000, ge=100, le=30000)
    data_root: Path = Path("./data")
    data_root_key: str = Field(default="default", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    max_scan_files: int = Field(default=1000, ge=1, le=10000)
    max_segments_per_call: int = Field(default=1000, ge=1, le=1000)
    admin_token: SecretStr | None = None
    session_minutes: int = Field(default=30, ge=1, le=1440)
    model_root: Path = Path("./models")
    worker_lease_seconds: float = Field(default=30, ge=0.2, le=3600)
    worker_heartbeat_seconds: float = Field(default=5, ge=0.05, le=300)
    worker_poll_seconds: float = Field(default=1, ge=0.05, le=60)
    worker_recovery_seconds: float = Field(default=5, ge=0.05, le=300)
    retry_base_seconds: float = Field(default=2, ge=0, le=60)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @model_validator(mode="after")
    def validate_heartbeat(self) -> "Settings":
        if self.worker_heartbeat_seconds >= self.worker_lease_seconds / 2:
            raise ValueError("Heartbeat interval must be less than half the lease duration")
        return self

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.database_user,
            password=self.database_password.get_secret_value(),
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
        )
