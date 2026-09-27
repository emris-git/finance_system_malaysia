"""Settings from environment (see .env.example)."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str
    # Signs dashboard links and cookies. Without it the dashboard login is off
    # (a known default key would let anyone forge a session).
    SECRET_KEY: str | None = None
    API_TOKEN: str | None = None
    TZ_NAME: str = "Asia/Kuala_Lumpur"
    # Your name as banks print it; lets transfers to yourself pair up automatically.
    OWNER_NAME: str | None = None

    TELEGRAM_BOT_TOKEN: str | None = None
    TELEGRAM_OWNER_ID: int | None = None
    TELEGRAM_WEBHOOK_SECRET: str | None = None
    PUBLIC_BASE_URL: str | None = None

    TNG_PDF_PASSWORD: str | None = None
    MAYBANK_PDF_PASSWORD: str | None = None

    SCHEDULER_ENABLED: bool = False

    # First day of the "financial month". Salary lands on the 25th, so a month
    # runs 26th -> 25th and its report goes out on the 26th. 1 = calendar months.
    MONTH_START_DAY: int = Field(26, ge=1, le=28)

    @field_validator("TELEGRAM_OWNER_ID", mode="before")
    @classmethod
    def _numeric_owner_id(cls, value):
        """A typo in the bot settings must not take the dashboard and API down with it."""
        if value in (None, ""):
            return None
        text = str(value).strip()
        if text.lstrip("-").isdigit():
            return int(text)
        logging.getLogger("finance.config").error(
            "TELEGRAM_OWNER_ID=%r is not a numeric Telegram user id (ask @userinfobot, "
            "a @username does not work); the bot stays silent until it is fixed",
            value,
        )
        return None

    @property
    def async_database_url(self) -> str:
        """Railway gives postgresql://; SQLAlchemy async needs the asyncpg driver."""
        url = self.DATABASE_URL
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+asyncpg://" + url[len(prefix):]
        return url

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.TZ_NAME)

    @property
    def pdf_passwords(self) -> list[str]:
        return [p for p in (self.TNG_PDF_PASSWORD, self.MAYBANK_PDF_PASSWORD) if p]


@lru_cache
def get_settings() -> Settings:
    return Settings()


CLIENT_ENV = Path.home() / ".config" / "finance" / "client.env"


class ClientSettings(BaseSettings):
    """For machines that only talk to the deployed API (the SSH host of the
    Apple Pay automation): no DATABASE_URL needed. Read from ./.env and
    ~/.config/finance/client.env (an SSH forced command runs in $HOME)."""

    model_config = SettingsConfigDict(env_file=(".env", CLIENT_ENV), extra="ignore")

    FINANCE_API_URL: str | None = None
    API_TOKEN: str | None = None
