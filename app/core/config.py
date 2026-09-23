from typing import Literal
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    _repo_root = Path(__file__).resolve().parents[2]
    model_config = SettingsConfigDict(
        env_file=(_repo_root.parent / ".env", _repo_root / ".env"),
        env_file_encoding="utf-8", extra="ignore")

    catalog_mode: Literal["demo", "live"] = "live"
    assistant_mode: Literal["demo", "openai"] = "openai"
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4.1-mini"
    ekt_base_url: Literal["https://ekt.kz"] = "https://ekt.kz"
    ekt_username: str = ""
    ekt_password: SecretStr = SecretStr("")
    cors_origins: list[str] = ["http://localhost:5500", "http://127.0.0.1:5500"]
    ekt_search_pages: int = Field(default=5, ge=1, le=50)
    request_timeout_seconds: float = Field(default=15, gt=0, le=120)
    chat_timeout_seconds: float = Field(default=45, gt=0, le=180)
    session_ttl_seconds: int = Field(default=3600, ge=60)
    max_sessions: int = Field(default=1000, ge=1, le=10000)

    @model_validator(mode="after")
    def validate_credentials(self):
        if self.catalog_mode == "live" and not (self.ekt_username and self.ekt_password.get_secret_value()):
            raise ValueError("Live catalog requires EKT_USERNAME and EKT_PASSWORD")
        if self.assistant_mode == "openai" and not self.openai_api_key.get_secret_value():
            raise ValueError("OpenAI assistant requires OPENAI_API_KEY")
        return self
