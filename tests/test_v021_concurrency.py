import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import select
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.models import Base, Account, TargetGroup, AudienceMember, InviteTask
from app.core.security import encrypt_session_string
from app.core.settings_service import (
    get_default_concurrency_mode,
    set_default_concurrency_mode,
    VALID_CONCURRENCY_MODES,
)
from app.bot.keyboards import inviter_config_keyboard
from app.services.inviter_service import InviterOrchestrator


@pytest.mark.asyncio
async def test_settings_concurrency_modes(test_session):
    assert await get_default_concurrency_mode(test_session) == "sequential"
    await set_default_concurrency_mode("sync_batch", test_session)
    assert await get_default_concurrency_mode(test_session) == "sync_batch"
    await set_default_concurrency_mode("parallel_async", test_session)
    assert await get_default_concurrency_mode(test_session) == "parallel_async"

    with pytest.raises(ValueError):
        await set_default_concurrency_mode("invalid_mode", test_session)


def test_inviter_config_keyboard_concurrency_labels():
    kb_seq = inviter_config_keyboard(selected_limit=10, current_profile="normal", concurrency_mode="sequential")
    buttons_seq = [btn.text for row in kb_seq.inline_keyboard for btn in row]
    assert "• По очереди" in buttons_seq
    assert "Асинхронно" in buttons_seq
    assert "Сразу все" in buttons_seq

    kb_batch = inviter_config_keyboard(selected_limit=10, current_profile="normal", concurrency_mode="sync_batch")
    buttons_batch = [btn.text for row in kb_batch.inline_keyboard for btn in row]
    assert "По очереди" in buttons_batch
    assert "• Сразу все" in buttons_batch

    kb_async = inviter_config_keyboard(selected_limit=10, current_profile="normal", concurrency_mode="parallel_async")
    buttons_async = [btn.text for row in kb_async.inline_keyboard for btn in row]
    assert "• Асинхронно" in buttons_async


@pytest.mark.asyncio
async def test_worker_stats_and_identity_formatting():
    orch = InviterOrchestrator(task_id=1, concurrency_mode="sequential")

    # Record successful invite
    line1, status1 = await orch._record_attempt(
        invite_success=True,
        error_status=None,
        error_reason=None,
        account_label="+48723339243",
        user_tag="@durov",
    )
    assert "Пользователь @durov добавлен через +48723339243" in line1
    assert "• <code>@durov</code> - добавлен (+48723339243)" in status1
    assert orch.worker_stats["+48723339243"] == 1

    # Record another successful invite from second worker
    line2, status2 = await orch._record_attempt(
        invite_success=True,
        error_status=None,
        error_reason=None,
        account_label="+48723339244",
        user_tag="@alex",
    )
    assert "Пользователь @alex добавлен через +48723339244" in line2
    assert "• <code>@alex</code> - добавлен (+48723339244)" in status2
    assert orch.worker_stats["+48723339244"] == 1
    assert "+48723339243: 1" in status2
    assert "+48723339244: 1" in status2

    # Record skipped/error invite
    line3, status3 = await orch._record_attempt(
        invite_success=False,
        error_status="restricted",
        error_reason="Приватность пользователя",
        account_label="+48723339243",
        user_tag="@private_user",
    )
    assert "пропуск (Приватность пользователя) [+48723339243]" in line3
    assert "• <code>@private_user</code> - пропуск (Приватность пользователя) [+48723339243]" in status3
    # worker_stats only increments on success
    assert orch.worker_stats["+48723339243"] == 1


@pytest.mark.asyncio
async def test_sync_batch_mode_execution():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        for i in range(3):
            session.add(Account(
                phone=f"+4870000000{i}",
                session_encrypted=encrypt_session_string("dummy_session"),
                status="active",
                is_active=True,
            ))
        target = TargetGroup(
            title="Target Chat",
            username="target_chat",
            tg_id=-1001234567890,
            access_hash=987654321,
            chat_type="supergroup",
        )
        session.add(target)
        await session.flush()
        for i in range(3):
            session.add(AudienceMember(
                tg_id=4000 + i,
                access_hash=5000 + i,
                username=f"member_{i}",
                source_chat="donor",
                status="pending",
            ))
        task = InviteTask(
            target_group_id=target.id,
            speed_profile="fast",
            concurrency_mode="sync_batch",
            status="pending",
            total_targets=3,
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    orchestrator = InviterOrchestrator(task_id=task_id, concurrency_mode="sync_batch")
    inviting_workers = []

    def mock_client_factory(account, proxy=None):
        async def successful_invite(*args, **kwargs):
            inviting_workers.append(account.phone)
            res = MagicMock()
            res.missing_invitees = []
            res.users = []
            return res

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.iter_participants = MagicMock(return_value=[])
        client.side_effect = successful_invite
        return client

    with patch(
        "app.services.inviter_service.get_telethon_client",
        side_effect=mock_client_factory,
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

    assert len(inviting_workers) == 3
    assert set(inviting_workers) == {"+48700000000", "+48700000001", "+48700000002"}
    assert orchestrator.worker_stats["+48700000000"] == 1
    assert orchestrator.worker_stats["+48700000001"] == 1
    assert orchestrator.worker_stats["+48700000002"] == 1

    async with session_factory() as session:
        finished = await session.get(InviteTask, task_id)
        assert finished.status == "completed"
        assert finished.successful_invites == 3

    await engine.dispose()


@pytest.mark.asyncio
async def test_parallel_async_mode_execution(tmp_path):
    db_file = tmp_path / "test_concurrency.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_file}",
        connect_args={"timeout": 15},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        for i in range(2):
            session.add(Account(
                phone=f"+4871111111{i}",
                session_encrypted=encrypt_session_string("dummy_session"),
                status="active",
                is_active=True,
            ))
        target = TargetGroup(
            title="Target Chat",
            username="target_chat",
            tg_id=-1001234567890,
            access_hash=987654321,
            chat_type="supergroup",
        )
        session.add(target)
        await session.flush()
        for i in range(4):
            session.add(AudienceMember(
                tg_id=6000 + i,
                access_hash=7000 + i,
                username=f"target_async_{i}",
                source_chat="donor",
                status="pending",
            ))
        task = InviteTask(
            target_group_id=target.id,
            speed_profile="fast",
            concurrency_mode="parallel_async",
            status="pending",
            total_targets=4,
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    from telethon.tl.functions.channels import InviteToChannelRequest
    from telethon.tl.functions.messages import AddChatUserRequest

    orchestrator = InviterOrchestrator(task_id=task_id, concurrency_mode="parallel_async")
    invited_by_worker = {}

    def mock_client_factory(account, proxy=None):
        async def successful_invite(*args, **kwargs):
            await asyncio.sleep(0.01)
            req = args[0] if args else None
            if isinstance(req, (InviteToChannelRequest, AddChatUserRequest)):
                phone = account.phone
                invited_by_worker[phone] = invited_by_worker.get(phone, 0) + 1
            res = MagicMock()
            res.missing_invitees = []
            res.users = []
            return res

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.iter_participants = MagicMock(return_value=[])
        client.side_effect = successful_invite
        return client

    with patch(
        "app.services.inviter_service.get_telethon_client",
        side_effect=mock_client_factory,
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

    assert sum(invited_by_worker.values()) == 4
    assert len(invited_by_worker) == 2
    assert sum(orchestrator.worker_stats.values()) == 4

    async with session_factory() as session:
        finished = await session.get(InviteTask, task_id)
        assert finished.status == "completed"
        assert finished.successful_invites == 4
        invited_members = (await session.execute(
            select(AudienceMember).where(AudienceMember.status == "invited")
        )).scalars().all()
        assert len(invited_members) == 4
        for mem in invited_members:
            assert mem.invited_by_account_id is not None

    await engine.dispose()
