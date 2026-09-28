import json
from typing import Optional
from sqlalchemy import select
from app.core.config import settings
from app.core.database import async_session_factory
from app.models.models import AppSetting

MAX_DAILY_LIMIT = 500
VALID_SPEED_PROFILES = {"cautious", "normal", "fast"}

DAILY_LIMIT_KEY = "daily_invite_limit"
PRIVACY_BLACKLIST_KEY = "privacy_blacklist_enabled"
RECENT_ONLY_KEY = "recent_only_enabled"

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

SPEED_PROFILE_KEY = "speed_profile"

async def get_daily_invite_limit() -> int:
    val = await get_app_setting(DAILY_LIMIT_KEY)
    if val is not None:
        try:
            parsed = int(val)
            if 0 < parsed <= MAX_DAILY_LIMIT:
                return parsed
        except ValueError:
            pass
    return min(settings.MAX_INVITES_PER_SESSION_DAILY, MAX_DAILY_LIMIT)

async def set_daily_invite_limit(limit: int) -> None:
    if not 0 < limit <= MAX_DAILY_LIMIT:
        raise ValueError(f"Лимит должен быть от 1 до {MAX_DAILY_LIMIT}")
    await set_app_setting(DAILY_LIMIT_KEY, str(limit))

async def get_privacy_blacklist_enabled() -> bool:
    val = await get_app_setting(PRIVACY_BLACKLIST_KEY, default="true")
    return str(val).lower() in ("true", "1", "yes")

async def set_privacy_blacklist_enabled(enabled: bool) -> None:
    await set_app_setting(PRIVACY_BLACKLIST_KEY, "true" if enabled else "false")

async def get_recent_only_enabled() -> bool:
    val = await get_app_setting(RECENT_ONLY_KEY, default="false")
    return str(val).lower() in ("true", "1", "yes")

async def set_recent_only_enabled(enabled: bool) -> None:
    await set_app_setting(RECENT_ONLY_KEY, "true" if enabled else "false")

EXCLUDED_WORKERS_KEY = "excluded_worker_ids"

async def get_excluded_worker_ids() -> list[int]:
    val = await get_app_setting(EXCLUDED_WORKERS_KEY, default="[]")
    try:
        parsed = json.loads(val or "[]")
        return [int(v) for v in parsed if str(v).isdigit()]
    except (ValueError, TypeError):
        return []

async def set_worker_excluded(account_id: int, excluded: bool) -> None:
    current = set(await get_excluded_worker_ids())
    if excluded:
        current.add(account_id)
    else:
        current.discard(account_id)
    await set_app_setting(EXCLUDED_WORKERS_KEY, json.dumps(sorted(current)))

async def get_speed_profile() -> str:
    val = await get_app_setting(SPEED_PROFILE_KEY)
    if val and (val in VALID_SPEED_PROFILES or val.startswith("custom:")):
        return val
    return settings.DEFAULT_SPEED_PROFILE

async def set_speed_profile(profile: str) -> None:
    if profile not in VALID_SPEED_PROFILES and not profile.startswith("custom:"):
        raise ValueError(f"Unknown speed profile: {profile}")
    await set_app_setting(SPEED_PROFILE_KEY, profile)
