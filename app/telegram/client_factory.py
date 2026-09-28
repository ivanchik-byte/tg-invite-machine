import hashlib
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
    return decrypt_session_string(password)

def build_proxy_dict(proxy: Optional[Proxy]) -> Optional[Dict[str, Any]]:
    if not proxy:
        return None
    if not proxy.is_active:
        raise ProxySecurityError(f"Assigned proxy {proxy.host}:{proxy.port} is inactive")

    proto = proxy.protocol.lower() if proxy.protocol else "socks5"
    if proto not in ("socks5", "socks4", "http"):
        raise ProxySecurityError(f"Unsupported proxy protocol: {proxy.protocol}")

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

DEVICE_PROFILES = [
    {
        "device_model": "Desktop",
        "system_version": "Windows 11 Pro 64-bit",
        "app_version": "5.2.0 x64",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    },
    {
        "device_model": "Desktop",
        "system_version": "Windows 10 Pro 64-bit",
        "app_version": "5.1.7 x64",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    },
    {
        "device_model": "PC 64bit",
        "system_version": "Windows 11 Home",
        "app_version": "5.2.1 x64",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    },
    {
        "device_model": "MacBook Pro",
        "system_version": "macOS 14.4",
        "app_version": "10.9.1",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    },
    {
        "device_model": "iMac 27",
        "system_version": "macOS 14.2",
        "app_version": "10.8.3",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    },
]

def resolve_device_profile(account: Account) -> Dict[str, str]:
    model = getattr(account, "device_model", None)
    sys_ver = getattr(account, "system_version", None)
    if model and sys_ver:
        return {
            "device_model": model,
            "system_version": sys_ver,
            "app_version": getattr(account, "app_version", None) or "5.2.0 x64",
            "lang_code": getattr(account, "lang_code", None) or "ru",
            "system_lang_code": getattr(account, "system_lang_code", None) or "ru-RU",
        }

    seed = str(getattr(account, "phone", "") or getattr(account, "id", "") or "default")
    digest = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16)
    idx = digest % len(DEVICE_PROFILES)
    return DEVICE_PROFILES[idx]

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

    client_api_id = getattr(account, "api_id", None) or settings.TELEGRAM_API_ID
    client_api_hash = getattr(account, "api_hash", None) or settings.TELEGRAM_API_HASH

    profile = resolve_device_profile(account)

    client = TelegramClient(
        StringSession(decrypted_session),
        api_id=client_api_id,
        api_hash=client_api_hash,
        proxy=proxy_config,
        timeout=25,
        connection_retries=3,
        retry_delay=1,
        device_model=profile["device_model"],
        system_version=profile["system_version"],
        app_version=profile["app_version"],
        lang_code=profile["lang_code"],
        system_lang_code=profile["system_lang_code"]
    )
    return client
