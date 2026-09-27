import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
from telethon.sessions import StringSession
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from opentele2.td import TDesktop
from opentele2.api import CreateNewSession

from app.core.config import settings
from app.core.security import safe_extract_zip, encrypt_session_string
from app.telegram.client_factory import ProxySecurityError

# returned as msg when the tdata archive or session needs a 2fa password
PASSWORD_REQUIRED = "tdata_password_required"

async def import_session_file(
    session_path: Path,
    password: Optional[str] = None,
    proxy: Optional[Dict[str, Any]] = None
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    if settings.REQUIRE_STRICT_PROXIES and not proxy:
        raise ProxySecurityError("Zero-leak policy: Active proxy is required for account import")

    session_name = str(session_path.resolve())
    client = TelegramClient(
        session_name,
        api_id=settings.TELEGRAM_API_ID,
        api_hash=settings.TELEGRAM_API_HASH,
        proxy=proxy
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            if password:
                try:
                    await client.sign_in(password=password)
                except Exception as sign_in_err:
                    return False, f"Ошибка 2FA пароля: {sign_in_err}", None
            else:
                return False, "Сессия не авторизована или отозвана в Telegram", None

        user = await client.get_me()
        session_string = StringSession.save(client.session)
        encrypted_token = encrypt_session_string(session_string)

        user_info = {
            "phone": getattr(user, "phone", None) or session_path.stem,
            "first_name": getattr(user, "first_name", None),
            "last_name": getattr(user, "last_name", None),
            "username": getattr(user, "username", None),
            "session_encrypted": encrypted_token,
            "two_fa_password": password,
            "api_id": settings.TELEGRAM_API_ID,
            "api_hash": settings.TELEGRAM_API_HASH
        }
        return True, "Успешно", user_info
    except SessionPasswordNeededError:
        return False, PASSWORD_REQUIRED, None
    except Exception as exc:
        if "password" in str(exc).lower():
            return False, PASSWORD_REQUIRED, None
        return False, f"Ошибка чтения сессии: {exc}", None
    finally:
        await client.disconnect()

async def convert_tdata_archive(
    zip_path: Path,
    password: Optional[str] = None,
    proxy: Optional[Dict[str, Any]] = None
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    if settings.REQUIRE_STRICT_PROXIES and not proxy:
        raise ProxySecurityError("Zero-leak policy: Active proxy is required for account import")

    temp_dir = Path(tempfile.mkdtemp(prefix="tdata_"))
    try:
        await asyncio.to_thread(safe_extract_zip, zip_path, temp_dir)

        tdata_dir = None
        for current_root, directories, _ in os.walk(temp_dir):
            for directory in directories:
                if directory.lower() == "tdata":
                    tdata_dir = Path(current_root) / directory
                    break
            if tdata_dir:
                break

        target_dir = tdata_dir or temp_dir
        tdesk = await asyncio.to_thread(TDesktop, str(target_dir))

        if not tdesk.isLoaded() or not tdesk.accounts:
            return False, "Архив не содержит валидных данных авторизации Telegram", None

        telethon_client: TelegramClient = await tdesk.ToTelethon(
            flag=CreateNewSession,
            password=password,
            proxy=proxy
        )
        try:
            if not telethon_client.is_connected():
                await telethon_client.connect()

            if not await telethon_client.is_user_authorized():
                return False, "Не удалось авторизовать новую сессию из TData", None

            user = await telethon_client.get_me()
            session_string = StringSession.save(telethon_client.session)
            encrypted_token = encrypt_session_string(session_string)

            api_id = getattr(telethon_client, "api_id", None) or 2040
            api_hash = getattr(telethon_client, "api_hash", None) or "b18441a1ff607e10a989891a5462e627"

            account_info = {
                "phone": getattr(user, "phone", None) or f"+{user.id}",
                "first_name": getattr(user, "first_name", None),
                "last_name": getattr(user, "last_name", None),
                "username": getattr(user, "username", None),
                "session_encrypted": encrypted_token,
                "two_fa_password": password,
                "api_id": api_id,
                "api_hash": api_hash
            }
            return True, "Успешно", account_info
        finally:
            await telethon_client.disconnect()
    except Exception as exc:
        exc_name = type(exc).__name__.lower()
        exc_str = str(exc).lower()
        if "nopasswordprovided" in exc_name or "sessionpasswordneeded" in exc_name or "password" in exc_str:
            return False, PASSWORD_REQUIRED, None
        return False, f"Ошибка конвертации TData: {exc}", None
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
