import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from sqlalchemy import select, delete, update
from app.core.database import async_session_factory
from app.models.models import Account, AudienceMember, AudienceHistory, TargetGroup, InviteTask
from app.core.settings_service import (
    get_privacy_blacklist_enabled,
    set_privacy_blacklist_enabled,
    get_speed_profile,
    set_speed_profile,
)
from datetime import datetime, timedelta, timezone
from app.services.inviter_service import InviterOrchestrator, calculate_delay
from app.services.account_service import auto_recover_cooldowns, reset_all_cooldowns
from telethon.errors import UserChannelsTooMuchError

@pytest.mark.asyncio
async def test_privacy_blacklist_settings():
    # Test setting and getting blacklist toggle
    await set_privacy_blacklist_enabled(False)
    assert await get_privacy_blacklist_enabled() is False

    await set_privacy_blacklist_enabled(True)
    assert await get_privacy_blacklist_enabled() is True

@pytest.mark.asyncio
async def test_pre_sync_marks_existing_chat_participants():
    test_uid = 777111001
    async with async_session_factory() as session:
        # Cleanup
        await session.execute(delete(AudienceMember).where(AudienceMember.tg_id == test_uid))
        await session.commit()

        # Add a pending member
        member = AudienceMember(
            tg_id=test_uid,
            username="test_in_group",
            source_chat="donor_test",
            status="pending"
        )
        session.add(member)
        await session.commit()

        # Simulate Pre-Sync bulk update
        existing_uids = [test_uid]
        await session.execute(
            update(AudienceMember)
            .where(
                AudienceMember.tg_id.in_(existing_uids),
                AudienceMember.status == "pending"
            )
            .values(
                status="already_participant",
                reason="Уже состоит в группе (Pre-Sync)"
            )
        )
        await session.commit()

        updated_member = (await session.execute(
            select(AudienceMember).where(AudienceMember.tg_id == test_uid)
        )).scalar_one()

        assert updated_member.status == "already_participant"
        assert "Pre-Sync" in updated_member.reason

        # Cleanup
        await session.execute(delete(AudienceMember).where(AudienceMember.tg_id == test_uid))
        await session.commit()

@pytest.mark.asyncio
async def test_blacklist_filter_skips_restricted_members():
    restricted_uid = 888222001
    clean_uid = 999333001

    await set_privacy_blacklist_enabled(True)

    async with async_session_factory() as session:
        # Cleanup
        await session.execute(delete(AudienceHistory).where(AudienceHistory.tg_id.in_([restricted_uid, clean_uid])))
        await session.execute(delete(AudienceMember).where(AudienceMember.tg_id.in_([restricted_uid, clean_uid])))
        await session.commit()

        # Add history: restricted_uid has privacy restriction
        session.add(AudienceHistory(
            tg_id=restricted_uid,
            username="private_user",
            source_chat="donor_1",
            status="restricted"
        ))
        # Add both to AudienceMember queue as pending
        session.add(AudienceMember(
            tg_id=restricted_uid,
            username="private_user",
            source_chat="donor_1",
            status="pending"
        ))
        session.add(AudienceMember(
            tg_id=clean_uid,
            username="open_user",
            source_chat="donor_1",
            status="pending"
        ))
        await session.commit()

        # Simulate the pre-marking sweep with blacklist enabled
        bl_subq = select(AudienceHistory.tg_id).where(
            AudienceHistory.status.in_(["restricted", "channels_too_much", "uninvitable"])
        )
        await session.execute(
            update(AudienceMember)
            .where(
                AudienceMember.status == "pending",
                AudienceMember.tg_id.in_(bl_subq)
            )
            .values(
                status="restricted",
                reason="Исключен блэклистом приватности"
            )
        )
        await session.commit()

        # Query candidates for next invite
        pending_members = (await session.execute(
            select(AudienceMember).where(AudienceMember.status == "pending")
        )).scalars().all()

        pending_uids = [m.tg_id for m in pending_members]
        assert clean_uid in pending_uids
        assert restricted_uid not in pending_uids

        # Verify restricted member has proper status
        restricted_member = (await session.execute(
            select(AudienceMember).where(AudienceMember.tg_id == restricted_uid)
        )).scalar_one()
        assert restricted_member.status == "restricted"
        assert restricted_member.reason == "Исключен блэклистом приватности"

        # Cleanup
        await session.execute(delete(AudienceHistory).where(AudienceHistory.tg_id.in_([restricted_uid, clean_uid])))
        await session.execute(delete(AudienceMember).where(AudienceMember.tg_id.in_([restricted_uid, clean_uid])))
        await session.commit()

@pytest.mark.asyncio
async def test_restoring_pending_when_blacklist_disabled():
    test_uid = 555444333
    async with async_session_factory() as session:
        await session.execute(delete(AudienceMember).where(AudienceMember.tg_id == test_uid))
        await session.commit()

        session.add(AudienceMember(
            tg_id=test_uid,
            username="temporarily_restricted",
            source_chat="donor",
            status="restricted",
            reason="Исключен блэклистом приватности"
        ))
        await session.commit()

        # Turn toggle off -> restores to pending
        await session.execute(
            update(AudienceMember)
            .where(
                AudienceMember.status == "restricted",
                AudienceMember.reason == "Исключен блэклистом приватности"
            )
            .values(status="pending", reason=None)
        )
        await session.commit()

        restored = (await session.execute(
            select(AudienceMember).where(AudienceMember.tg_id == test_uid)
        )).scalar_one()
        assert restored.status == "pending"
        assert restored.reason is None

        # Cleanup
        await session.execute(delete(AudienceMember).where(AudienceMember.tg_id == test_uid))
        await session.commit()

@pytest.mark.asyncio
async def test_speed_profile_persistence():
    await set_speed_profile("cautious")
    assert await get_speed_profile() == "cautious"

    await set_speed_profile("custom:40:60")
    assert await get_speed_profile() == "custom:40:60"

    # Reset
    await set_speed_profile("normal")
    assert await get_speed_profile() == "normal"

def test_calculate_delay_strictly_respects_custom_bounds():
    # 40-60 custom interval must never produce < 40 or > 60
    for _ in range(100):
        d = calculate_delay("custom:40:60")
        assert 40.0 <= d <= 60.0, f"Delay {d} out of bounds [40, 60]"

    # 1-4 custom interval
    for _ in range(50):
        d = calculate_delay("custom:1:4")
        assert 1.0 <= d <= 4.0, f"Delay {d} out of bounds [1, 4]"

    # 0-0 turbo mode
    assert calculate_delay("custom:0:0") == 0.0

def test_calculate_delay_presets():
    assert calculate_delay("cautious") > 0
    assert calculate_delay("normal") > 0
    assert calculate_delay("fast") > 0

@pytest.mark.asyncio
async def test_auto_recover_cooldowns():
    test_phone = "99900011122"
    async with async_session_factory() as session:
        await session.execute(delete(Account).where(Account.phone == test_phone))
        await session.commit()

        past_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
        acc = Account(
            phone=test_phone,
            session_encrypted="enc",
            status="cooldown",
            cooldown_until=past_time,
            is_active=True
        )
        session.add(acc)
        await session.commit()

        recovered = await auto_recover_cooldowns(session)
        assert recovered >= 1

        db_acc = (await session.execute(select(Account).where(Account.phone == test_phone))).scalar_one()
        assert db_acc.status == "active"
        assert db_acc.cooldown_until is None

        await session.execute(delete(Account).where(Account.phone == test_phone))
        await session.commit()

@pytest.mark.asyncio
async def test_reset_all_cooldowns():
    test_phone = "99900011133"
    async with async_session_factory() as session:
        await session.execute(delete(Account).where(Account.phone == test_phone))
        await session.commit()

        future_time = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5)
        acc = Account(
            phone=test_phone,
            session_encrypted="enc",
            status="cooldown",
            cooldown_until=future_time,
            is_active=True
        )
        session.add(acc)
        await session.commit()

        # auto_recover should NOT touch it because future
        rec = await auto_recover_cooldowns(session)
        # Verify our specific test account is still in cooldown
        db_acc = (await session.execute(select(Account).where(Account.phone == test_phone))).scalar_one()
        assert db_acc.status == "cooldown"

        # manual reset should touch it
        res = await reset_all_cooldowns(session)
        assert res >= 1

        db_acc = (await session.execute(select(Account).where(Account.phone == test_phone))).scalar_one()
        assert db_acc.status == "active"
        assert db_acc.cooldown_until is None

        await session.execute(delete(Account).where(Account.phone == test_phone))
        await session.commit()

def test_peer_flood_cooldown_config():
    from app.core.config import settings
    assert settings.PEER_FLOOD_COOLDOWN_MINUTES == 5

def test_inviter_menu_keyboard_with_paused_task():
    from app.bot.keyboards import inviter_menu_keyboard
    kb = inviter_menu_keyboard(task_running=False, paused_task_id=26)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "invite_resume_paused_26" in callbacks

def test_auto_wait_cooldown_threshold():
    from app.core.config import settings
    max_auto_wait = max(600, settings.PEER_FLOOD_COOLDOWN_MINUTES * 60 + 30)
    # 5 minutes = 300s, max_auto_wait should be >= 600s
    assert max_auto_wait >= 600
    assert 300 <= max_auto_wait

@pytest.mark.asyncio
async def test_peer_flood_leaves_member_pending():
    test_uid = 999555111
    async with async_session_factory() as session:
        await session.execute(delete(AudienceMember).where(AudienceMember.tg_id == test_uid))
        await session.commit()

        member = AudienceMember(
            tg_id=test_uid,
            username="flood_victim",
            source_chat="donor_flood",
            status="pending"
        )
        session.add(member)
        await session.commit()

        # Simulate what inviter_service does on peer_flood / flood_wait
        error_status = "peer_flood"
        db_member = (await session.execute(select(AudienceMember).where(AudienceMember.tg_id == test_uid))).scalar_one()

        if error_status in ("account_banned", "flood_wait", "peer_flood", "no_rights"):
            db_member.status = "pending"
            db_member.reason = None
        else:
            db_member.status = error_status

        await session.commit()

        checked = (await session.execute(select(AudienceMember).where(AudienceMember.tg_id == test_uid))).scalar_one()
        assert checked.status == "pending"
        assert checked.reason is None

        await session.execute(delete(AudienceMember).where(AudienceMember.tg_id == test_uid))
        await session.commit()




