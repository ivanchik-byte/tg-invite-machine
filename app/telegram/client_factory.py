from typing import Optional, Dict, Any
from telethon import TelegramClient
from telethon.sessions import StringSession

from app.core.config import settings
from app.core.security import decrypt_session_string
from app.models.models import Account, Proxy

class ProxySecurityError(Exception):
    """Raised when an active proxy is required to prevent host IP leaks."""
    pass

def decrypt_proxy_password(password: Optional[str]) -> Optional[str]:
    if not password:
        return None
    try:
        return decrypt_session_string(password)
    except Exception:
        return password

def build_proxy_dict(proxy: Optional[Proxy]) -> Optional[Dict[str, Any]]:
    if not proxy:
        return None
    if not proxy.is_active:
        raise ProxySecurityError(f"Assigned proxy {proxy.host}:{proxy.port} is inactive")

    proto = proxy.protocol.lower() if proxy.protocol else "socks5"
    if proto not in ("socks5", "socks4", "http"):
        proto = "socks5"

    proxy_config: Dict[str, Any] = {
        "proxy_type": proto,
        "addr": proxy.host,
        "port": proxy.port,
        "rdns": True,
    }
    if proxy.username:
        proxy_config["username"] = proxy.username
    if proxy.password:
        proxy_config["password"] = decrypt_proxy_password(proxy.password)

    return proxy_config

def get_telethon_client(account: Account, proxy: Optional[Proxy] = None) -> TelegramClient:
    resolved_proxy = proxy
    if resolved_proxy is None and hasattr(account, "proxy"):
        resolved_proxy = account.proxy

    proxy_config = build_proxy_dict(resolved_proxy)
    if settings.REQUIRE_STRICT_PROXIES and not proxy_config:
        raise ProxySecurityError(
            f"Zero-leak policy: Account {account.phone} does not have an active proxy configured."
        )

    decrypted_session = decrypt_session_string(account.session_encrypted)

    client = TelegramClient(
        StringSession(decrypted_session),
        api_id=settings.TELEGRAM_API_ID,
        api_hash=settings.TELEGRAM_API_HASH,
        proxy=proxy_config,
        device_model="Desktop",
        system_version="Windows 11 Pro 64-bit",
        app_version="5.1.7 x64",
        lang_code="ru",
        system_lang_code="ru-RU"
    )
    return client
