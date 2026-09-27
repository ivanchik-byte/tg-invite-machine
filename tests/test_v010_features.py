import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.models import Base, Account, TargetGroup, AudienceMember, InviteTask, Proxy
from app.services.account_service import register_single_account
from app.services.inviter_service import calculate_delay, InviterOrchestrator
from app.core.security import decrypt_session_string, encrypt_session_string
from app.bot.keyboards import inviter_config_keyboard, speed_profile_keyboard
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from app.bot.handlers.inviter import handle_custom_limit, handle_custom_delay


def test_calculate_delay_custom_profile():
    # Valid range 40-80: bounds are [40 * 0.7, 80 * 1.5] = [28, 120]
    for _ in range(50):
        delay = calculate_delay("custom:40:80")
        assert 28.0 <= delay <= 120.0

    # Inverted bounds 80:40 should be swapped internally
    for _ in range(20):
        delay = calculate_delay("custom:80:40")
        assert 28.0 <= delay <= 120.0

    # Malformed custom string falls back safely
    delay_bad = calculate_delay("custom:not_a_number:foo")
    assert delay_bad > 0


@pytest.mark.asyncio
async def test_account_2fa_encryption_and_registration():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    raw_2fa = "SuperSecret_2FA_123!"
    info = {
        "phone": "+79991234567",
        "first_name": "TestUser",
        "username": "test_2fa_user",
        "session_encrypted": encrypt_session_string("mock_session_data")
    }

    async with session_factory() as session:
        acc, is_new = await register_single_account(
            session=session,
            info=info,
            two_fa_password=raw_2fa
        )
        assert is_new is True
        assert acc.two_fa_password is not None
        assert acc.two_fa_password != raw_2fa
        assert decrypt_session_string(acc.two_fa_password) == raw_2fa

        # Updating account preserves/updates 2FA
        info_updated = dict(info)
        info_updated["first_name"] = "UpdatedName"
        updated_acc, is_new_again = await register_single_account(
            session=session,
            info=info_updated,
            two_fa_password="NewPassword456!"
        )
        assert is_new_again is False
        assert updated_acc.first_name == "UpdatedName"
        assert decrypt_session_string(updated_acc.two_fa_password) == "NewPassword456!"

    await engine.dispose()


@pytest.mark.asyncio
async def test_invite_task_stops_at_max_invites():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        proxy = Proxy(host="127.0.0.1", port=9050, protocol="socks5", is_active=True)
        session.add(proxy)
        await session.flush()

        acc = Account(
            phone="+79990001122",
            session_encrypted=encrypt_session_string("dummy_session"),
            status="active",
            is_active=True,
            proxy_id=proxy.id
        )
        session.add(acc)

        target = TargetGroup(
            title="Target Chat",
            username="target_chat",
            tg_id=-1001234567890,
            access_hash=987654321,
            chat_type="supergroup"
        )
        session.add(target)
        await session.flush()

        # Add 5 pending members
        for i in range(1, 6):
            member = AudienceMember(
                tg_id=1000 + i,
                username=f"user_{i}",
                source_chat="source_chat",
                status="pending",
                target_group_id=target.id
            )
            session.add(member)

        task = InviteTask(
            target_group_id=target.id,
            speed_profile="fast",
            max_invites=2,
            status="pending",
            total_targets=5
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    orchestrator = InviterOrchestrator(task_id=task_id)

    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()
    mock_client.disconnect = AsyncMock()
    mock_client.get_entity = AsyncMock(return_value=MagicMock())
    mock_client.__call__ = AsyncMock(return_value=True)

    with patch("app.services.inviter_service.get_telethon_client", return_value=mock_client), \
         patch("app.services.inviter_service.simulate_pre_invite_reading", AsyncMock()), \
         patch("app.services.inviter_service.calculate_delay", return_value=0.001):
        await orchestrator.run(session_factory=session_factory)

    async with session_factory() as session:
        completed_task = await session.get(InviteTask, task_id)
        assert completed_task.status == "completed"
        assert completed_task.successful_invites == 2
        assert completed_task.finished_at is not None

        # Verify only 2 members were invited, others remain pending
        invited_count = (await session.execute(
            select(AudienceMember).where(AudienceMember.status == "invited")
        )).scalars().all()
        assert len(invited_count) == 2

    await engine.dispose()


def test_inviter_config_keyboard_layout():
    # Preset: 20 selected, normal speed
    kb = inviter_config_keyboard(selected_limit=20, current_profile="normal")
    button_texts = [btn.text for row in kb.inline_keyboard for btn in row]

    assert "• 20" in button_texts
    assert "5" in button_texts
    assert "10" in button_texts
    assert "• Обычный (35-75с)" in button_texts
    assert "Запустить инвайтинг" in button_texts
    assert "Отмена" in button_texts

    # Preset: All selected, custom speed
    kb_custom = inviter_config_keyboard(selected_limit=None, current_profile="custom:40:80")
    button_texts_custom = [btn.text for row in kb_custom.inline_keyboard for btn in row]

    assert "• Все" in button_texts_custom
    assert "• Свой интервал: 40-80с" in button_texts_custom

    # Custom limit selected: e.g. 15
    kb_lim15 = inviter_config_keyboard(selected_limit=15, current_profile="cautious")
    button_texts_lim15 = [btn.text for row in kb_lim15.inline_keyboard for btn in row]
    assert "• Свой лимит: 15" in button_texts_lim15
    assert "• Осторожный (50-110с)" in button_texts_lim15


def test_speed_profile_keyboard_includes_custom():
    kb = speed_profile_keyboard()
    button_texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert "Свой интервал" in button_texts


@pytest.mark.asyncio
async def test_custom_limit_handler_valid():
    msg = MagicMock(spec=Message)
    msg.text = "42"
    msg.answer = AsyncMock()

    state = MagicMock(spec=FSMContext)
    state.get_data = AsyncMock(return_value={
        "target_group_id": 1,
        "target_link": "@target_test",
        "chat_type": "supergroup",
        "speed_profile": "normal"
    })
    state.update_data = AsyncMock()

    with patch("app.bot.handlers.inviter._show_pre_launch_config", AsyncMock()) as mock_show:
        await handle_custom_limit(msg, state)
        mock_show.assert_awaited_once()
        _, kwargs = mock_show.call_args
        assert kwargs["selected_limit"] == 42


@pytest.mark.asyncio
async def test_custom_limit_handler_invalid():
    msg = MagicMock(spec=Message)
    msg.text = "not_a_number"
    msg.answer = AsyncMock()

    state = MagicMock(spec=FSMContext)

    await handle_custom_limit(msg, state)
    msg.answer.assert_awaited_once()
    assert "корректное число" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_custom_delay_handler_valid_and_invalid():
    # Valid range 45-90
    msg = MagicMock(spec=Message)
    msg.text = "45-90"
    msg.answer = AsyncMock()

    state = MagicMock(spec=FSMContext)
    state.get_data = AsyncMock(return_value={
        "target_group_id": 1,
        "target_link": "@target_test",
        "chat_type": "supergroup",
        "selected_limit": 20,
        "source": "task_config"
    })
    state.update_data = AsyncMock()

    with patch("app.bot.handlers.inviter._show_pre_launch_config", AsyncMock()) as mock_show:
        await handle_custom_delay(msg, state)
        mock_show.assert_awaited_once()
        _, kwargs = mock_show.call_args
        assert kwargs["speed_profile"] == "custom:45:90"

    # Invalid range out of bounds
    msg_inv = MagicMock(spec=Message)
    msg_inv.text = "1-2"
    msg_inv.answer = AsyncMock()
    await handle_custom_delay(msg_inv, state)
    msg_inv.answer.assert_awaited_once()
    assert "корректный диапазон" in msg_inv.answer.call_args[0][0]
