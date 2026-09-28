from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

class Proxy(Base):
    __tablename__ = "proxies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    protocol: Mapped[str] = mapped_column(String(16), default="socks5")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    accounts: Mapped[List["Account"]] = relationship("Account", back_populates="proxy")

    @property
    def url(self) -> str:
        if self.username:
            return f"{self.protocol}://{self.username}:***@{self.host}:{self.port}"
        return f"{self.protocol}://{self.host}:{self.port}"

class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    session_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    first_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    proxy_id: Mapped[Optional[int]] = mapped_column(ForeignKey("proxies.id", ondelete="SET NULL"), nullable=True)
    proxy: Mapped[Optional[Proxy]] = relationship("Proxy", back_populates="accounts")

    two_fa_password: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    api_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    api_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    daily_invites_count: Mapped[int] = mapped_column(Integer, default=0)
    last_invite_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    cooldown_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    flood_incidents: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    def get_effective_daily_invites(self) -> int:
        today = utc_now().date()
        if self.last_invite_at and self.last_invite_at.date() < today:
            return 0
        return self.daily_invites_count

    def record_invite(self, now: Optional[datetime] = None) -> None:
        timestamp = now or utc_now()
        if self.last_invite_at and self.last_invite_at.date() < timestamp.date():
            self.daily_invites_count = 1
        else:
            self.daily_invites_count += 1
        self.last_invite_at = timestamp

class TargetGroup(Base):
    __tablename__ = "target_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tg_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True, nullable=True)
    access_hash: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # null for targets created before type detection
    chat_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    @property
    def target_link(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.title or f"Chat #{self.id}"

class AudienceMember(Base):
    __tablename__ = "audience_members"
    __table_args__ = (
        UniqueConstraint("tg_id", "source_chat", name="uq_audience_tg_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True, nullable=True)
    access_hash: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    source_chat: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    invited_by_account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    target_group_id: Mapped[Optional[int]] = mapped_column(ForeignKey("target_groups.id", ondelete="CASCADE"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

class InviteTask(Base):
    __tablename__ = "invite_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_group_id: Mapped[int] = mapped_column(ForeignKey("target_groups.id", ondelete="CASCADE"), nullable=False)
    target_group: Mapped[Optional[TargetGroup]] = relationship("TargetGroup")
    speed_profile: Mapped[str] = mapped_column(String(32), default="normal")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    max_invites: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_targets: Mapped[int] = mapped_column(Integer, default=0)
    successful_invites: Mapped[int] = mapped_column(Integer, default=0)
    restricted_count: Mapped[int] = mapped_column(Integer, default=0)
    flood_errors: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    @property
    def success_count(self) -> int:
        return self.successful_invites

    @property
    def flood_waits_count(self) -> int:
        return self.flood_errors

class AudienceHistory(Base):
    __tablename__ = "audience_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True, index=True, nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    source_chat: Mapped[str] = mapped_column(String(255), default="unknown", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="collected", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

