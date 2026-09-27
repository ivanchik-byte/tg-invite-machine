from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, model_validator

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
TEMP_DIR = BASE_DIR / "temp"

DATA_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

    BOT_TOKEN: str = Field(default="", description="Telegram Bot Token")
    ADMIN_ID: int = Field(default=0, description="Telegram User ID of Bot Owner")

    TELEGRAM_API_ID: int = Field(default=0, description="MTProto API ID")
    TELEGRAM_API_HASH: str = Field(default="", description="MTProto API HASH")

    ENCRYPTION_KEY: str = Field(default="", description="Fernet encryption key for sessions")
    REQUIRE_STRICT_PROXIES: bool = Field(default=True, description="Strict fail-fast proxy check to prevent IP leaks")
    DATABASE_URL: str = Field(
        default=f"sqlite+aiosqlite:///{DATA_DIR / 'inviter.db'}",
        description="Async database connection string"
    )

    DEFAULT_SPEED_PROFILE: str = Field(default="normal", description="cautious | normal | fast")
    MIN_DELAY_BETWEEN_INVITES: int = Field(default=35, description="Min seconds between invites")
    MAX_DELAY_BETWEEN_INVITES: int = Field(default=75, description="Max seconds between invites")
    ANTI_BURST_WORKER_DELAY: int = Field(default=40, description="Global delay between worker invites")
    MAX_INVITES_PER_SESSION_DAILY: int = Field(default=20, description="Daily limit per session")

    CIRCUIT_BREAKER_FLOOD_THRESHOLD: int = Field(default=3, description="Consecutive floods to trigger freeze")
    CIRCUIT_BREAKER_FREEZE_MINUTES: int = Field(default=60, description="Minutes to freeze on flood")

    PRE_INVITE_READ_POSTS: bool = Field(default=True, description="Emulate reading chat before inviting")
    APP_LIKE_BEHAVIOR: bool = Field(default=True, description="Emulate opening app and dialogs")

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        if self.MIN_DELAY_BETWEEN_INVITES > self.MAX_DELAY_BETWEEN_INVITES:
            raise ValueError("MIN_DELAY_BETWEEN_INVITES cannot exceed MAX_DELAY_BETWEEN_INVITES")
        if self.DEFAULT_SPEED_PROFILE not in {"cautious", "normal", "fast"}:
            raise ValueError(f"Invalid DEFAULT_SPEED_PROFILE: {self.DEFAULT_SPEED_PROFILE}")
        return self


settings = Settings()
