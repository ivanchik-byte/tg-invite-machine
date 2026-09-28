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
    REQUIRE_STRICT_PROXIES: bool = Field(default=False, description="Strict fail-fast proxy check to prevent IP leaks")

    @property
    def FAIL_FAST_ON_PROXY_ERROR(self) -> bool:
        return self.REQUIRE_STRICT_PROXIES

    DATABASE_URL: str = Field(
        default=f"sqlite+aiosqlite:///{DATA_DIR / 'inviter.db'}",
        description="Async database connection string"
    )

    DEFAULT_SPEED_PROFILE: str = Field(default="normal", description="cautious | normal | fast")
    MIN_DELAY_BETWEEN_INVITES: int = Field(default=35, description="Min seconds between invites")
    MAX_DELAY_BETWEEN_INVITES: int = Field(default=75, description="Max seconds between invites")
    MAX_INVITES_PER_SESSION_DAILY: int = Field(default=20, description="Daily limit per session")

    CIRCUIT_BREAKER_FLOOD_THRESHOLD: int = Field(default=3, description="Consecutive floods to trigger freeze")
    PEER_FLOOD_COOLDOWN_HOURS: int = Field(default=12, description="Hours to cooldown an account on PeerFlood")

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        if self.MIN_DELAY_BETWEEN_INVITES > self.MAX_DELAY_BETWEEN_INVITES:
            raise ValueError("MIN_DELAY_BETWEEN_INVITES cannot exceed MAX_DELAY_BETWEEN_INVITES")
        if self.DEFAULT_SPEED_PROFILE not in {"cautious", "normal", "fast"}:
            raise ValueError(f"Invalid DEFAULT_SPEED_PROFILE: {self.DEFAULT_SPEED_PROFILE}")
        missing = [k for k, v in {
            "BOT_TOKEN": self.BOT_TOKEN,
            "TELEGRAM_API_ID": self.TELEGRAM_API_ID,
            "TELEGRAM_API_HASH": self.TELEGRAM_API_HASH,
            "ENCRYPTION_KEY": self.ENCRYPTION_KEY,
        }.items() if not v]
        if missing:
            raise ValueError(f"Required settings not configured: {', '.join(missing)}")
        try:
            from cryptography.fernet import Fernet
            Fernet(self.ENCRYPTION_KEY.encode() if isinstance(self.ENCRYPTION_KEY, str) else self.ENCRYPTION_KEY)
        except Exception as exc:
            raise ValueError(f"ENCRYPTION_KEY is not a valid Fernet key: {exc}") from exc
        return self


settings = Settings()
