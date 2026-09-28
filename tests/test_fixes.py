import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.models import Base
from app.services.proxy_service import import_proxies_from_text


@pytest.mark.asyncio
async def test_proxy_dedup_distinguishes_protocol_and_user():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        added, _ = await import_proxies_from_text(session, "10.0.0.1:1080:user1:pass1\n10.0.0.1:1080:user2:pass2")
        assert added == 2

    async with factory() as session:
        added, skipped = await import_proxies_from_text(session, "10.0.0.1:1080:user1:pass1")
        assert added == 0
        assert skipped == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_proxy_dedup_same_endpoint_twice_in_batch():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        added, skipped = await import_proxies_from_text(
            session, "10.0.0.2:1080:user1:pass1\n10.0.0.2:1080:user1:pass1"
        )
        assert added == 1
        assert skipped == 1
    await engine.dispose()


def test_dashboard_reexport_keeps_handler_imports_working():
    from app.bot.handlers.menu import build_main_dashboard_text, build_stats_text
    from app.bot import dashboard
    assert dashboard.build_main_dashboard_text is build_main_dashboard_text
    assert dashboard.build_stats_text is build_stats_text


def test_task_manager_stop_keeps_handle_until_done():
    import asyncio
    from app.services.task_manager import InviteTaskManager

    async def scenario():
        manager = InviteTaskManager()

        async def worker():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise

        orch = type("Orch", (), {"stop": lambda self: None})()
        manager.start(7, orch, worker())
        assert manager.is_running()
        assert manager.stop() is True
        await asyncio.sleep(0.05)
        assert manager.active_task_handle is None
        assert manager.active_task_id is None

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_channel_invalid_error_excludes_worker_and_leaves_target_pending():
    from unittest.mock import AsyncMock, patch, MagicMock
    from telethon.errors import ChannelInvalidError
    from telethon.tl.types.messages import InvitedUsers
    from telethon.tl.types import Updates
    from app.core.security import encrypt_session_string
    from app.models.models import Account, TargetGroup, AudienceMember, InviteTask
    from app.services.inviter_service import InviterOrchestrator

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        a1 = Account(
            phone="+1111111111",
            session_encrypted=encrypt_session_string("session1"),
            status="active",
            is_active=True,
        )
        a2 = Account(
            phone="+2222222222",
            session_encrypted=encrypt_session_string("session2"),
            status="active",
            is_active=True,
        )
        target = TargetGroup(
            title="Target Chat",
            username="target_chat",
            tg_id=-1001234567890,
            access_hash=987654321,
            chat_type="supergroup",
        )
        session.add_all([a1, a2, target])
        await session.flush()

        member = AudienceMember(
            tg_id=9999,
            username="Sciomancer",
            source_chat="donor_chat",
            status="pending",
        )
        session.add(member)

        task = InviteTask(
            target_group_id=target.id,
            speed_profile="fast",
            max_invites=1,
            status="running",
            total_targets=1,
        )
        session.add(task)
        await session.commit()
        task_id = task.id
        a1_id = a1.id
        a2_id = a2.id

    orchestrator = InviterOrchestrator(task_id=task_id)

    mock_user = MagicMock()
    mock_user.id = 9999

    res_success = InvitedUsers(
        updates=Updates(updates=[], users=[mock_user], chats=[], date=None, seq=0),
        missing_invitees=[],
    )

    def factory(account, proxy=None):
        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.get_entity = AsyncMock(return_value=mock_user)

        async def fake_call(req):
            if account.id == a1_id:
                raise ChannelInvalidError(None)
            return res_success

        client.side_effect = fake_call
        return client

    with patch(
        "app.services.inviter_service.get_telethon_client",
        side_effect=factory,
    ), patch(
        "app.services.inviter_service.simulate_pre_invite_reading", AsyncMock()
    ), patch(
        "app.services.inviter_service.calculate_delay", return_value=0.001
    ), patch(
        "app.services.inviter_service.get_recent_only_enabled", AsyncMock(return_value=False)
    ), patch(
        "app.services.inviter_service.get_excluded_worker_ids", AsyncMock(return_value=[])
    ), patch(
        "app.services.inviter_service.get_privacy_blacklist_enabled", AsyncMock(return_value=False)
    ):
        await orchestrator.run(session_factory=session_factory)

    async with session_factory() as session:
        t = await session.get(InviteTask, task_id)
        assert t.status == "completed"
        assert t.successful_invites == 1

        db_m = (await session.execute(select(AudienceMember).where(AudienceMember.tg_id == 9999))).scalar_one()
        assert db_m.status == "invited"
        assert db_m.invited_by_account_id == a2_id

    await engine.dispose()
