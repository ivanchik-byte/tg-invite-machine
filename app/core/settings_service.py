from typing import Optional
from sqlalchemy import select
from app.core.config import settings
from app.core.database import async_session_factory
from app.models.models import AppSetting

DAILY_LIMIT_KEY = "daily_invite_limit"

async def get_app_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    async with async_session_factory() as session:
        setting = (await session.execute(
            select(AppSetting).where(AppSetting.key == key)
        )).scalars().first()
        if setting:
            return setting.value
        return default

async def set_app_setting(key: str, value: str) -> None:
    async with async_session_factory() as session:
        setting = (await session.execute(
            select(AppSetting).where(AppSetting.key == key)
        )).scalars().first()
        if setting:
            setting.value = value
        else:
            session.add(AppSetting(key=key, value=value))
        await session.commit()

async def get_daily_invite_limit() -> int:
    val = await get_app_setting(DAILY_LIMIT_KEY)
    if val is not None:
        try:
            parsed = int(val)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    return settings.MAX_INVITES_PER_SESSION_DAILY

async def set_daily_invite_limit(limit: int) -> None:
    if limit <= 0:
        raise ValueError("Лимит должен быть положительным числом")
    await set_app_setting(DAILY_LIMIT_KEY, str(limit))
