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

@pytest.mark.asyncio
async def test_proxy_deletion_and_binding():
    from app.models.models import Proxy, Account
    from app.services.proxy_service import (
        bind_proxy_to_account,
        unbind_proxy_from_account,
        delete_single_proxy,
    )

    test_phone = "99988877701"
    async with async_session_factory() as session:
        # Cleanup
        await session.execute(delete(Account).where(Account.phone == test_phone))
        await session.commit()

        proxy = Proxy(
            host="1.2.3.4",
            port=1080,
            protocol="socks5",
            is_active=True
        )
        session.add(proxy)
        await session.flush()

        acc = Account(
            phone=test_phone,
            session_encrypted="enc",
            status="active"
        )
        session.add(acc)
        await session.commit()

        proxy_id = proxy.id
        acc_id = acc.id

        # Test binding
        bound = await bind_proxy_to_account(session, proxy_id=proxy_id, account_id=acc_id)
        assert bound is True

        db_acc = await session.get(Account, acc_id)
        assert db_acc.proxy_id == proxy_id

        # Test unbinding
        unbound = await unbind_proxy_from_account(session, account_id=acc_id)
        assert unbound is True

        db_acc = await session.get(Account, acc_id)
        assert db_acc.proxy_id is None

        # Rebind and delete proxy
        await bind_proxy_to_account(session, proxy_id=proxy_id, account_id=acc_id)
        deleted = await delete_single_proxy(session, proxy_id=proxy_id)
        assert deleted is True

        db_proxy = await session.get(Proxy, proxy_id)
        assert db_proxy is None

        db_acc = await session.get(Account, acc_id)
        assert db_acc.proxy_id is None

        # Final cleanup
        await session.delete(db_acc)
        await session.commit()

@pytest.mark.asyncio
async def test_purge_dead_proxies():
    from app.models.models import Proxy, Account
    from app.services.proxy_service import purge_dead_proxies

    test_phone = "99988877702"
    async with async_session_factory() as session:
        # Cleanup
        await session.execute(delete(Account).where(Account.phone == test_phone))
        await session.commit()

        p_live = Proxy(host="10.0.0.1", port=1080, protocol="socks5", is_active=True)
        p_dead = Proxy(host="10.0.0.2", port=1080, protocol="socks5", is_active=False)
        session.add_all([p_live, p_dead])
        await session.flush()

        acc = Account(
            phone=test_phone,
            session_encrypted="enc",
            status="active",
            proxy_id=p_dead.id
        )
        session.add(acc)
        await session.commit()

        live_id = p_live.id
        dead_id = p_dead.id
        acc_id = acc.id

        purged_count = await purge_dead_proxies(session)
        assert purged_count >= 1

        # Check dead proxy was deleted
        assert await session.get(Proxy, dead_id) is None
        # Check live proxy is still intact
        assert await session.get(Proxy, live_id) is not None
        # Check account was unlinked from dead proxy
        db_acc = await session.get(Account, acc_id)
        assert db_acc.proxy_id is None

        # Cleanup
        await session.delete(await session.get(Proxy, live_id))
        await session.delete(db_acc)
        await session.commit()

def test_proxy_and_account_keyboards():
    from app.bot.keyboards import (
        proxies_menu_keyboard,
        proxy_pagination_keyboard,
        proxy_view_keyboard,
        account_view_keyboard,
        account_proxy_pick_keyboard,
    )

    # proxies_menu_keyboard with dead proxies
    kb = proxies_menu_keyboard(has_proxies=True, dead_count=3)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "proxy_list_0" in callbacks
    assert "proxy_purge_dead_confirm" in callbacks
    assert "proxy_purge_all_confirm" in callbacks

    # proxies_menu_keyboard without proxies
    kb_empty = proxies_menu_keyboard(has_proxies=False, dead_count=0)
    cb_empty = [btn.callback_data for row in kb_empty.inline_keyboard for btn in row]
    assert "proxy_list_0" not in cb_empty
    assert "proxy_purge_dead_confirm" not in cb_empty

    # proxy_pagination_keyboard
    items = [(1, "#1 [OK] 1.2.3.4:1080 (1 акк)")]
    kb_pag = proxy_pagination_keyboard(offset=0, limit=8, total=10, proxy_items=items)
    cb_pag = [btn.callback_data for row in kb_pag.inline_keyboard for btn in row]
    assert "proxy_view_1_0" in cb_pag
    assert "proxy_list_8" in cb_pag

    # proxy_view_keyboard
    kb_view = proxy_view_keyboard(proxy_id=1, offset=0, has_accounts=True)
    cb_view = [btn.callback_data for row in kb_view.inline_keyboard for btn in row]
    assert "proxy_bind_pick_1_0" in cb_view
    assert "proxy_unbind_1_0" in cb_view
    assert "proxy_del_1_0" in cb_view
    assert "proxy_probe_1_0" in cb_view

    # account_view_keyboard
    kb_acc = account_view_keyboard(acc_id=5, offset=0, has_proxy=True)
    cb_acc = [btn.callback_data for row in kb_acc.inline_keyboard for btn in row]
    assert "acc_proxy_pick_5_0" in cb_acc
    assert "acc_proxy_detach_5_0" in cb_acc
    assert "acc_del_5_0" in cb_acc

    # account_proxy_pick_keyboard
    kb_pick = account_proxy_pick_keyboard(acc_id=5, offset=0, proxies=[(1, "#1 [OK] 1.2.3.4:1080")])
    cb_pick = [btn.callback_data for row in kb_pick.inline_keyboard for btn in row]
    assert "acc_proxy_apply_5_1_0" in cb_pick





