import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from telethon.errors import UserPrivacyRestrictedError

from app.models.models import Base, Account, TargetGroup, AudienceMember, InviteTask, utc_now
from app.core.security import encrypt_session_string
from app.core.settings_service import get_recent_only_enabled, set_recent_only_enabled
from app.bot.keyboards import inviter_config_keyboard
from app.services.inviter_service import InviterOrchestrator


@pytest.mark.asyncio
async def test_failed_attempts_rotate_workers():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        for i in range(3):
            session.add(Account(
                phone=f"+7999000110{i}",
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
        for i in range(6):
            session.add(AudienceMember(
                tg_id=2000 + i,
                access_hash=3000 + i,
                source_chat="donor",
                status="pending",
            ))
        task = InviteTask(
            target_group_id=target.id,
            speed_profile="fast",
            status="pending",
            total_targets=6,
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    orchestrator = InviterOrchestrator(task_id=task_id)
    used_workers = []

    def counting_factory(account, proxy=None):
        async def failing_invite(*args, **kwargs):
            used_workers.append(account.id)
            raise UserPrivacyRestrictedError(None)

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.iter_participants = MagicMock(return_value=[])
        client.side_effect = failing_invite
        return client

    with patch(
        "app.services.inviter_service.get_telethon_client",
        side_effect=counting_factory,
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

    assert len(used_workers) == 6
    async with session_factory() as session:
        expected_worker_ids = set((await session.execute(select(Account.id))).scalars().all())
    assert set(used_workers) == expected_worker_ids
    from collections import Counter
    counts = Counter(used_workers)
    assert max(counts.values()) - min(counts.values()) <= 1

    async with session_factory() as session:
        finished = await session.get(InviteTask, task_id)
        assert finished.status == "completed"
        assert finished.successful_invites == 0
        stamped = (await session.execute(
            select(Account).where(Account.last_attempt_at.is_not(None))
        )).scalars().all()
        assert len(stamped) == 3
        restricted = (await session.execute(
            select(AudienceMember).where(AudienceMember.status == "restricted")
        )).scalars().all()
        assert len(restricted) == 6

    await engine.dispose()


@pytest.mark.asyncio
async def test_recent_only_skips_stale_members():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        session.add(Account(
            phone="+79990002211",
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
        session.add(AudienceMember(
            tg_id=3001, access_hash=4001, source_chat="donor",
            last_seen_at=utc_now(), status="pending",
        ))
        session.add(AudienceMember(
            tg_id=3002, access_hash=4002, source_chat="donor",
            last_seen_at=None, status="pending",
        ))
        task = InviteTask(
            target_group_id=target.id, speed_profile="fast",
            status="pending", total_targets=2,
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    orchestrator = InviterOrchestrator(task_id=task_id)

    def success_factory(account, proxy=None):
        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.iter_participants = MagicMock(return_value=[])
        client.side_effect = AsyncMock(return_value=True)
        return client

    with patch(
        "app.services.inviter_service.get_telethon_client",
        side_effect=success_factory,
    ), patch(
        "app.services.inviter_service.simulate_pre_invite_reading", AsyncMock()
    ), patch(
        "app.services.inviter_service.calculate_delay", return_value=0.001
    ), patch(
        "app.services.inviter_service.get_recent_only_enabled", AsyncMock(return_value=True)
    ), patch(
        "app.services.inviter_service.get_excluded_worker_ids", AsyncMock(return_value=[])
    ), patch(
        "app.services.inviter_service.get_privacy_blacklist_enabled", AsyncMock(return_value=False)
    ):
        await orchestrator.run(session_factory=session_factory)

    async with session_factory() as session:
        fresh = (await session.execute(
            select(AudienceMember).where(AudienceMember.tg_id == 3001)
        )).scalar_one()
        stale = (await session.execute(
            select(AudienceMember).where(AudienceMember.tg_id == 3002)
        )).scalar_one()
        assert fresh.status == "invited"
        assert stale.status == "pending"
        finished = await session.get(InviteTask, task_id)
        assert finished.status == "completed"
        assert finished.successful_invites == 1

    await engine.dispose()


def test_config_keyboard_mode_marks():
    carousel_kb = inviter_config_keyboard(selected_limit=None, current_profile="normal")
    carousel_texts = [b.text for row in carousel_kb.inline_keyboard for b in row]
    assert "• Карусель (Safe)" in carousel_texts
    assert "Целевой план (Target)" in carousel_texts

    target_kb = inviter_config_keyboard(selected_limit=10, current_profile="normal")
    target_texts = [b.text for row in target_kb.inline_keyboard for b in row]
    assert "• Целевой план (Target)" in target_texts
    assert "Карусель (Safe)" in target_texts


@pytest.mark.asyncio
async def test_excluded_workers_never_picked():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        for i in range(2):
            session.add(Account(
                phone=f"+7999000330{i}",
                session_encrypted=encrypt_session_string("dummy_session"),
                status="active",
                is_active=True,
            ))
        target = TargetGroup(
            title="Target Chat", username="target_chat",
            tg_id=-1001234567890, access_hash=987654321,
            chat_type="supergroup",
        )
        session.add(target)
        await session.flush()
        for i in range(3):
            session.add(AudienceMember(
                tg_id=4000 + i, access_hash=5000 + i,
                source_chat="donor", status="pending",
            ))
        task = InviteTask(
            target_group_id=target.id, speed_profile="fast",
            status="pending", total_targets=3,
        )
        session.add(task)
        await session.commit()
        task_id = task.id
        worker_ids = [r[0] for r in (await session.execute(select(Account.id).order_by(Account.id))).all()]

    orchestrator = InviterOrchestrator(task_id=task_id)
    used_workers = []

    def counting_factory(account, proxy=None):
        async def succeeding_invite(*args, **kwargs):
            used_workers.append(account.id)
            return True

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.iter_participants = MagicMock(return_value=[])
        client.side_effect = succeeding_invite
        return client

    with patch(
        "app.services.inviter_service.get_telethon_client",
        side_effect=counting_factory,
    ), patch(
        "app.services.inviter_service.simulate_pre_invite_reading", AsyncMock()
    ), patch(
        "app.services.inviter_service.calculate_delay", return_value=0.001
    ), patch(
        "app.services.inviter_service.get_excluded_worker_ids",
        AsyncMock(return_value=[worker_ids[0]]),
    ), patch(
        "app.services.inviter_service.get_recent_only_enabled", AsyncMock(return_value=False)
    ), patch(
        "app.services.inviter_service.get_privacy_blacklist_enabled", AsyncMock(return_value=False)
    ):
        await orchestrator.run(session_factory=session_factory)

    assert used_workers
    assert set(used_workers) == {worker_ids[1]}

    async with session_factory() as session:
        finished = await session.get(InviteTask, task_id)
        assert finished.successful_invites == 3

    await engine.dispose()


@pytest.mark.asyncio
async def test_target_mode_stops_at_max_invites_with_interleaved_failures():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        for i in range(2):
            session.add(Account(
                phone=f"+7999000550{i}",
                session_encrypted=encrypt_session_string("dummy_session"),
                status="active",
                is_active=True,
            ))
        target = TargetGroup(
            title="Target Chat", username="target_chat",
            tg_id=-1001234567890, access_hash=987654321,
            chat_type="supergroup",
        )
        session.add(target)
        await session.flush()
        # 4 candidates: 1st fails, 2nd succeeds, 3rd fails, 4th succeeds
        for i in range(4):
            session.add(AudienceMember(
                tg_id=6000 + i, access_hash=7000 + i,
                source_chat="donor", status="pending",
            ))
        task = InviteTask(
            target_group_id=target.id, speed_profile="fast",
            status="pending", total_targets=4, max_invites=2,
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    orchestrator = InviterOrchestrator(task_id=task_id)
    attempt_counter = 0

    def alternating_factory(account, proxy=None):
        nonlocal attempt_counter

        async def invite_action(*args, **kwargs):
            nonlocal attempt_counter
            attempt_counter += 1
            if attempt_counter in (1, 3):
                raise UserPrivacyRestrictedError(None)
            return True

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.iter_participants = MagicMock(return_value=[])
        client.side_effect = invite_action
        return client

    with patch(
        "app.services.inviter_service.get_telethon_client",
        side_effect=alternating_factory,
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

    assert attempt_counter == 4
    async with session_factory() as session:
        finished = await session.get(InviteTask, task_id)
        assert finished.status == "completed"
        assert finished.successful_invites == 2

    await engine.dispose()


def test_accounts_grid_two_columns():
    from app.bot.keyboards import accounts_grid_keyboard, account_view_keyboard
    entries = [(1, "[OK P#1]", "@alice"), (2, "[CD 4м]", "@bob"), (3, "[NO PRX]", "@carol")]
    kb = accounts_grid_keyboard(entries=entries, offset=0, limit=8, total=3)
    rows = kb.inline_keyboard
    assert len(rows[0]) == 2
    assert len(rows[1]) == 1
    assert rows[0][0].callback_data == "acc_view_1_0"

    # Test pagination buttons when multiple pages exist
    kb_paginated = accounts_grid_keyboard(entries=entries, offset=8, limit=8, total=20)
    pag_texts = [b.text for row in kb_paginated.inline_keyboard for b in row]
    assert "Назад" in pag_texts
    assert "Вперед" in pag_texts

    view_excl = account_view_keyboard(acc_id=1, offset=0, has_proxy=True, excluded=True)
    view_excl_texts = [b.text for row in view_excl.inline_keyboard for b in row]
    assert any("SKIP" in t for t in view_excl_texts)

    view_incl = account_view_keyboard(acc_id=1, offset=0, has_proxy=True, excluded=False)
    view_incl_texts = [b.text for row in view_incl.inline_keyboard for b in row]
    assert any("В задаче" in t for t in view_incl_texts)
