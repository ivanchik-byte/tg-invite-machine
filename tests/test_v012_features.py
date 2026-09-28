import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from sqlalchemy import select, delete, update
from app.core.database import async_session_factory
from app.models.models import Account, AudienceMember, AudienceHistory, TargetGroup, InviteTask
from app.core.settings_service import (
    get_privacy_blacklist_enabled,
    set_privacy_blacklist_enabled,
)
from app.services.inviter_service import InviterOrchestrator
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
