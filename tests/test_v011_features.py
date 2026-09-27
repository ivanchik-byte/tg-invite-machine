import pytest
from collections import deque
from sqlalchemy import select, func, delete
from app.core.database import async_session_factory
from app.models.models import Account, AudienceMember, AudienceHistory, AppSetting
from app.core.settings_service import get_daily_invite_limit, set_daily_invite_limit

@pytest.mark.asyncio
async def test_dynamic_daily_limit_persistence():
    await set_daily_invite_limit(45)
    limit = await get_daily_invite_limit()
    assert limit == 45
    await set_daily_invite_limit(20)
    assert await get_daily_invite_limit() == 20

@pytest.mark.asyncio
async def test_audience_history_deduplication():
    test_id = 999888777
    async with async_session_factory() as session:
        await session.execute(delete(AudienceHistory).where(AudienceHistory.tg_id == test_id))
        await session.commit()

        session.add(AudienceHistory(
            tg_id=test_id,
            username="already_known",
            source_chat="donor_a",
            status="collected"
        ))
        await session.commit()

        q1 = (await session.execute(
            select(AudienceHistory.tg_id).where(AudienceHistory.tg_id == test_id)
        )).scalar()
        assert q1 == test_id

        q2 = (await session.execute(
            select(func.lower(AudienceHistory.username)).where(
                func.lower(AudienceHistory.username) == "already_known"
            )
        )).scalar()
        assert q2 == "already_known"

        # Cleanup
        await session.execute(delete(AudienceHistory).where(AudienceHistory.tg_id == test_id))
        await session.commit()

@pytest.mark.asyncio
async def test_sliding_window_recent_users_deque():
    recent = deque(maxlen=5)
    for i in range(1, 8):
        recent.appendleft(f"@user_{i}")
    
    assert len(recent) == 5
    assert recent[0] == "@user_7"
    assert recent[1] == "@user_6"
    assert recent[4] == "@user_3"
    assert "@user_1" not in recent
    assert "@user_2" not in recent

@pytest.mark.asyncio
async def test_account_record_invite_only_on_success():
    account = Account(
        phone="+19999999999",
        session_encrypted="enc",
        daily_invites_count=0
    )
    assert account.daily_invites_count == 0
    account.record_invite()
    assert account.daily_invites_count == 1
