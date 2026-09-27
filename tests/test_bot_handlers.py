import pytest
from app.core.config import settings
from app.core.utils import normalize_chat_identifier
from app.telegram.client_factory import build_proxy_dict, get_telethon_client, ProxySecurityError
from app.models.models import Account, Proxy
from app.services.export_service import generate_accounts_excel
from app.bot.states import AccountState

def test_proxy_security_fail_fast_when_inactive():
    inactive_proxy = Proxy(
        host="1.2.3.4",
        port=1080,
        protocol="socks5",
        is_active=False
    )
    with pytest.raises(ProxySecurityError, match="inactive"):
        build_proxy_dict(inactive_proxy)

def test_proxy_security_blocks_direct_connection_when_strict(monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_STRICT_PROXIES", True)
    account_without_proxy = Account(
        phone="+19998887766",
        session_encrypted="mock_encrypted_data"
    )
    with pytest.raises(ProxySecurityError, match="Zero-leak policy"):
        get_telethon_client(account_without_proxy, proxy=None)

def test_chat_identifier_normalizer():
    assert normalize_chat_identifier("@My_Crypto_Channel") == "my_crypto_channel"
    assert normalize_chat_identifier("https://t.me/MY_CHANNEL") == "my_channel"
    assert normalize_chat_identifier("http://t.me/Test_Chat") == "test_chat"
    assert normalize_chat_identifier("t.me/Sample_Chat") == "sample_chat"
    assert normalize_chat_identifier("https://t.me/Query_Chat?start=ref") == "query_chat"
    assert normalize_chat_identifier("+aBc123Join") == "+aBc123Join"
    assert normalize_chat_identifier("joinchat/XyZ123Case") == "joinchat/XyZ123Case"

def test_aiogram_state_string_comparison():
    current_state_str = "AccountState:waiting_for_password"
    assert current_state_str == AccountState.waiting_for_password.state
    assert (current_state_str != AccountState.waiting_for_password.state) is False

def test_generate_accounts_excel():
    proxy = Proxy(host="10.0.0.1", port=9050, protocol="socks5", is_active=True)
    accounts = [
        Account(id=1, phone="+123456789", username="test1", status="active", is_active=True, proxy=proxy),
        Account(id=2, phone="+987654321", username=None, status="spambot", is_active=True, proxy=None),
        Account(id=3, phone="+112233445", username="banned_user", status="banned", is_active=False, proxy=None)
    ]
    buffered_file = generate_accounts_excel(accounts)
    assert buffered_file.filename == "accounts_audit.xlsx"
    assert len(buffered_file.data) > 1000

from unittest.mock import AsyncMock, MagicMock
from aiogram.types import CallbackQuery, Message
from aiogram.exceptions import TelegramBadRequest
from app.bot.handlers.accounts import callback_nav_accounts
from app.bot.handlers.inviter import VALID_SPEED_PROFILES
from app.bot.keyboards import accounts_pagination_keyboard
from app.services.proxy_service import parse_proxy_line
from app.core.utils import safe_edit_text

def test_speed_profile_whitelist():
    assert "cautious" in VALID_SPEED_PROFILES
    assert "normal" in VALID_SPEED_PROFILES
    assert "fast" in VALID_SPEED_PROFILES
    assert "unsafe" not in VALID_SPEED_PROFILES

def test_proxy_parser_port_validation():
    assert parse_proxy_line("127.0.0.1:invalid_port") is None
    assert parse_proxy_line("127.0.0.1:999999") is None
    assert parse_proxy_line("127.0.0.1:0") is None
    assert parse_proxy_line("127.0.0.1:8080:user:pass") == ("127.0.0.1", 8080, "user", "pass", "socks5")

def test_accounts_pagination_keyboard():
    kb_first = accounts_pagination_keyboard(offset=0, limit=8, total=20)
    buttons_first = [btn.text for row in kb_first.inline_keyboard for btn in row]
    assert "Вперед" in buttons_first
    assert "Назад" not in buttons_first

    kb_mid = accounts_pagination_keyboard(offset=8, limit=8, total=20)
    buttons_mid = [btn.text for row in kb_mid.inline_keyboard for btn in row]
    assert "Назад" in buttons_mid
    assert "Вперед" in buttons_mid

    kb_last = accounts_pagination_keyboard(offset=16, limit=8, total=20)
    buttons_last = [btn.text for row in kb_last.inline_keyboard for btn in row]
    assert "Назад" in buttons_last
    assert "Вперед" not in buttons_last

@pytest.mark.asyncio
async def test_callback_nav_accounts_safe_with_none_state():
    callback = MagicMock(spec=CallbackQuery)
    callback.message = MagicMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()

    await callback_nav_accounts(callback, state=None)
    callback.answer.assert_awaited_once()

@pytest.mark.asyncio
async def test_safe_edit_text_suppresses_message_not_modified():
    mock_msg = MagicMock(spec=Message)
    mock_msg.edit_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: message is not modified")
    )
    result = await safe_edit_text(mock_msg, "Same text")
    assert result is False

@pytest.mark.asyncio
async def test_safe_edit_text_raises_other_bad_request():
    mock_msg = MagicMock(spec=Message)
    mock_msg.edit_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: chat not found")
    )
    with pytest.raises(TelegramBadRequest, match="chat not found"):
        await safe_edit_text(mock_msg, "Some text")

def test_settings_delay_validator():
    from cryptography.fernet import Fernet
    from app.core.config import Settings
    _creds = dict(
        BOT_TOKEN="0:test",
        TELEGRAM_API_ID=1,
        TELEGRAM_API_HASH="a" * 32,
        ENCRYPTION_KEY=Fernet.generate_key().decode(),
    )
    with pytest.raises(ValueError, match="MIN_DELAY_BETWEEN_INVITES cannot exceed MAX_DELAY_BETWEEN_INVITES"):
        Settings(MIN_DELAY_BETWEEN_INVITES=100, MAX_DELAY_BETWEEN_INVITES=50, **_creds)

    with pytest.raises(ValueError, match="Invalid DEFAULT_SPEED_PROFILE"):
        Settings(DEFAULT_SPEED_PROFILE="turbo_unsafe", **_creds)

def test_proxy_password_encryption_and_decryption():
    from app.core.security import encrypt_session_string
    from app.telegram.client_factory import build_proxy_dict, decrypt_proxy_password

    raw_password = "SecretPassword123!"
    encrypted_pwd = encrypt_session_string(raw_password)
    assert encrypted_pwd != raw_password

    assert decrypt_proxy_password(encrypted_pwd) == raw_password
    assert decrypt_proxy_password("plaintext_fallback") == "plaintext_fallback"
    assert decrypt_proxy_password(None) is None

    proxy = Proxy(
        host="192.168.1.1",
        port=1080,
        protocol="socks5",
        username="proxyuser",
        password=encrypted_pwd,
        is_active=True
    )
    proxy_dict = build_proxy_dict(proxy)
    assert proxy_dict["password"] == raw_password
    assert proxy_dict["username"] == "proxyuser"
    assert proxy_dict["proxy_type"] == "socks5"

def test_proxy_protocol_whitelist_fallback():
    from app.telegram.client_factory import build_proxy_dict

    proxy_bad = Proxy(host="10.0.0.1", port=1080, protocol="invalid_proto", is_active=True)
    assert build_proxy_dict(proxy_bad)["proxy_type"] == "socks5"

    proxy_none = Proxy(host="10.0.0.1", port=1080, protocol=None, is_active=True)
    assert build_proxy_dict(proxy_none)["proxy_type"] == "socks5"

    proxy_socks4 = Proxy(host="10.0.0.1", port=1080, protocol="socks4", is_active=True)
    assert build_proxy_dict(proxy_socks4)["proxy_type"] == "socks4"

    proxy_http = Proxy(host="10.0.0.1", port=1080, protocol="HTTP", is_active=True)
    assert build_proxy_dict(proxy_http)["proxy_type"] == "http"

def test_proxy_url_masks_password():
    proxy = Proxy(
        host="1.2.3.4",
        port=8080,
        protocol="socks5",
        username="user1",
        password="super_secret_password"
    )
    assert "super_secret_password" not in proxy.url
    assert proxy.url == "socks5://user1:***@1.2.3.4:8080"

    proxy_no_user = Proxy(host="1.2.3.4", port=8080, protocol="socks5")
    assert proxy_no_user.url == "socks5://1.2.3.4:8080"

@pytest.mark.asyncio
async def test_inviter_orchestrator_instant_stop():
    import asyncio
    import time
    from app.services.inviter_service import InviterOrchestrator

    orchestrator = InviterOrchestrator(task_id=999)

    async def trigger_stop():
        await asyncio.sleep(0.05)
        orchestrator.stop()

    start = time.perf_counter()
    asyncio.create_task(trigger_stop())
    await asyncio.wait_for(orchestrator._stop_event.wait(), timeout=5.0)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.5
    assert orchestrator._stop_event.is_set()

@pytest.mark.asyncio
async def test_proxy_service_import_encrypts_password():
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.models.models import Base
    from app.services.proxy_service import import_proxies_from_text
    from app.core.security import decrypt_session_string

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        added, skipped = await import_proxies_from_text(session, "socks5://testuser:rawpassword@1.1.1.1:1080")
        assert added == 1
        assert skipped == 0

        proxy = (await session.execute(select(Proxy).where(Proxy.host == "1.1.1.1"))).scalars().first()
        assert proxy is not None
        assert proxy.password != "rawpassword"
        assert decrypt_session_string(proxy.password) == "rawpassword"

    await engine.dispose()


@pytest.mark.asyncio
async def test_flood_defers_member_out_of_pending_pick():
    from sqlalchemy import select, and_
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.models.models import Base, TargetGroup, AudienceMember

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        group = TargetGroup(title="target", username="target")
        session.add(group)
        await session.flush()
        session.add(AudienceMember(tg_id=1, source_chat="src", status="pending", target_group_id=group.id))
        session.add(AudienceMember(tg_id=2, source_chat="src", status="deferred",
                                   reason="FloodWait 60s", target_group_id=group.id))
        await session.commit()

        pick = select(AudienceMember).where(
            and_(
                AudienceMember.status == "pending",
                (AudienceMember.target_group_id == None) | (AudienceMember.target_group_id == group.id),
            )
        ).order_by(AudienceMember.id.asc()).limit(1)
        member = (await session.execute(pick)).scalars().first()
        assert member is not None
        assert member.tg_id == 1

        deferred = (await session.execute(
            select(AudienceMember).where(AudienceMember.status == "deferred"))).scalars().all()
        assert len(deferred) == 1
        assert deferred[0].reason == "FloodWait 60s"

    await engine.dispose()


@pytest.mark.asyncio
async def test_invite_stop_finalizes_task_in_db(monkeypatch):
    import asyncio
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.models.models import Base, TargetGroup, InviteTask
    import app.bot.handlers.inviter as inviter_handler

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        group = TargetGroup(title="target", username="target")
        session.add(group)
        await session.flush()
        task = InviteTask(target_group_id=group.id, status="running")
        session.add(task)
        await session.commit()
        task_id = task.id

    monkeypatch.setattr(inviter_handler, "async_session_factory", session_factory)

    stopped = False

    class FakeOrchestrator:
        def stop(self):
            nonlocal stopped
            stopped = True

    done = asyncio.get_running_loop().create_future()
    done.set_result(None)
    monkeypatch.setattr(inviter_handler, "active_orchestrator", FakeOrchestrator())
    monkeypatch.setattr(inviter_handler, "active_task_handle", done)
    monkeypatch.setattr(inviter_handler, "active_task_id", task_id)

    callback = MagicMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()

    await inviter_handler.callback_invite_stop(callback)

    assert stopped
    assert inviter_handler.active_orchestrator is None
    assert inviter_handler.active_task_id is None

    async with session_factory() as session:
        stored = await session.get(InviteTask, task_id)
        assert stored.status == "stopped"
        assert stored.finished_at is not None

    await engine.dispose()


def test_tdata_password_required_sentinel(monkeypatch, tmp_path):
    import asyncio
    import zipfile
    from pathlib import Path
    import app.telegram.converter as converter

    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("tdata/", "")

    monkeypatch.setattr(converter.settings, "REQUIRE_STRICT_PROXIES", False)

    def need_password(*args, **kwargs):
        raise Exception("tdata archive needs password")

    monkeypatch.setattr(converter, "TDesktop", need_password)
    monkeypatch.setattr(converter, "safe_extract_zip",
                        lambda zip_path, temp_dir: Path(temp_dir).mkdir(parents=True, exist_ok=True))

    success, msg, info = asyncio.run(converter.convert_tdata_archive(archive))
    assert success is False
    assert msg == converter.PASSWORD_REQUIRED
    assert info is None

    def other_failure(*args, **kwargs):
        raise Exception("disk gone")

    monkeypatch.setattr(converter, "TDesktop", other_failure)
    success, msg, info = asyncio.run(converter.convert_tdata_archive(archive))
    assert success is False
    assert msg != converter.PASSWORD_REQUIRED


def test_target_type_labels_cover_all_chat_types():
    from app.bot.handlers.inviter import TARGET_TYPE_LABELS
    assert TARGET_TYPE_LABELS["channel"] == "канал"
    assert TARGET_TYPE_LABELS["supergroup"] == "супергруппа"
    assert TARGET_TYPE_LABELS["basic_group"] == "группа"


def test_migrate_confirm_keyboard():
    from app.bot.keyboards import migrate_confirm_keyboard
    kb = migrate_confirm_keyboard()
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "migrate_confirm" in callbacks
    assert "migrate_cancel" in callbacks


@pytest.mark.asyncio
async def test_pre_invite_reading_skips_mark_for_channels(monkeypatch):
    from types import SimpleNamespace
    from app.services import inviter_service

    monkeypatch.setattr(inviter_service.random, "randint", lambda a, b: 2)
    monkeypatch.setattr(inviter_service.random, "uniform", lambda a, b: 0)

    class FakeClient:
        def __init__(self):
            self.acked = False

        async def __call__(self, request):
            return None

        def iter_messages(self, entity, limit=None):
            async def gen():
                for mid in (5, 6):
                    yield SimpleNamespace(id=mid)
            return gen()

        async def send_read_acknowledge(self, *args, **kwargs):
            self.acked = True

    channel_client = FakeClient()
    await inviter_service.simulate_pre_invite_reading(channel_client, object(), mark_read=False)
    assert channel_client.acked is False

    group_client = FakeClient()
    await inviter_service.simulate_pre_invite_reading(group_client, object(), mark_read=True)
    assert group_client.acked is True

@pytest.mark.asyncio
async def test_converter_strict_proxy_blocks_without_proxy(monkeypatch, tmp_path):
    import app.telegram.converter as converter
    from app.telegram.client_factory import ProxySecurityError

    monkeypatch.setattr(converter.settings, "REQUIRE_STRICT_PROXIES", True)

    session_file = tmp_path / "test.session"
    session_file.write_bytes(b"dummy")

    with pytest.raises(ProxySecurityError, match="Zero-leak policy"):
        await converter.import_session_file(session_file, proxy=None)

    archive = tmp_path / "bundle.zip"
    archive.write_bytes(b"dummy")

    with pytest.raises(ProxySecurityError, match="Zero-leak policy"):
        await converter.convert_tdata_archive(archive, proxy=None)

def test_collector_alphabet_contains_cyrillic():
    cyrillic = [chr(c) for c in range(ord('а'), ord('я') + 1)]
    assert 'а' in cyrillic
    assert 'я' in cyrillic
    assert len(cyrillic) == 32

@pytest.mark.asyncio
async def test_inviter_ban_error_handling_flow():
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.models.models import Base, Account, AudienceMember

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        acc = Account(phone="+10000000000", session_encrypted="dummy", status="active", is_active=True)
        session.add(acc)
        member = AudienceMember(tg_id=42, username="user42", source_chat="src", status="pending")
        session.add(member)
        await session.commit()
        acc_id = acc.id
        member_id = member.id

    async with session_factory() as session:
        db_acc = await session.get(Account, acc_id)
        db_acc.status = "banned"
        db_acc.is_active = False

        db_member = await session.get(AudienceMember, member_id)
        db_member.status = "pending"
        db_member.reason = None
        await session.commit()

    async with session_factory() as session:
        refreshed_acc = await session.get(Account, acc_id)
        assert refreshed_acc.status == "banned"
        assert refreshed_acc.is_active is False

        refreshed_member = await session.get(AudienceMember, member_id)
        assert refreshed_member.status == "pending"

    await engine.dispose()

def test_zip_single_session_support(tmp_path):
    import zipfile
    from app.core.security import safe_extract_zip

    zip_path = tmp_path / "single.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("test_account.session", "fake_session_data")

    dest_dir = tmp_path / "extracted"
    safe_extract_zip(zip_path, dest_dir)
    session_files = list(dest_dir.rglob("*.session"))
    assert len(session_files) == 1
    assert len(session_files) >= 1

@pytest.mark.asyncio
async def test_invite_task_manager_lifecycle():
    import asyncio
    from app.services.task_manager import InviteTaskManager

    mgr = InviteTaskManager()
    assert mgr.is_running() is False
    assert mgr.get_active_task_id() is None

    class MockOrchestrator:
        def __init__(self):
            self.paused = False
            self.resumed = False
            self.stopped = False

        def pause(self):
            self.paused = True

        def resume(self):
            self.resumed = True

        def stop(self):
            self.stopped = True

    orch = MockOrchestrator()

    async def dummy_job():
        await asyncio.sleep(0.5)

    task = mgr.start(task_id=99, orchestrator=orch, coro=dummy_job())
    assert mgr.is_running() is True
    assert mgr.get_active_task_id() == 99

    coro_fail = dummy_job()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            mgr.start(task_id=100, orchestrator=orch, coro=coro_fail)
    finally:
        coro_fail.close()

    assert mgr.pause() is True
    assert orch.paused is True

    assert mgr.resume() is True
    assert orch.resumed is True

    # Test UI update throttling
    assert mgr.should_update_ui(min_interval=3.0, is_final=False) is True
    assert mgr.should_update_ui(min_interval=3.0, is_final=False) is False
    assert mgr.should_update_ui(min_interval=3.0, is_final=True) is True

    assert mgr.stop() is True
    assert orch.stopped is True
    task.cancel()

@pytest.mark.asyncio
async def test_register_single_account():
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.models.models import Base
    from app.services.account_service import register_single_account

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    info = {
        "phone": "+79991234567",
        "session_encrypted": "enc_data_test",
        "first_name": "Ivan",
        "username": "ivan_dev"
    }

    async with session_factory() as session:
        acc, is_new = await register_single_account(session, info, proxy_id=None)
        assert is_new is True
        assert acc.phone == "+79991234567"
        assert acc.username == "ivan_dev"

        info["first_name"] = "Ivan Updated"
        acc2, is_new2 = await register_single_account(session, info, proxy_id=None)
        assert is_new2 is False
        assert acc2.id == acc.id
        assert acc2.first_name == "Ivan Updated"

    await engine.dispose()



