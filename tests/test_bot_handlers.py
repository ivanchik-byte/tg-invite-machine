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
    from app.core.config import Settings
    with pytest.raises(ValueError, match="MIN_DELAY_BETWEEN_INVITES cannot exceed MAX_DELAY_BETWEEN_INVITES"):
        Settings(MIN_DELAY_BETWEEN_INVITES=100, MAX_DELAY_BETWEEN_INVITES=50)

    with pytest.raises(ValueError, match="Invalid DEFAULT_SPEED_PROFILE"):
        Settings(DEFAULT_SPEED_PROFILE="turbo_unsafe")
