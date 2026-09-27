import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
from telethon.sessions import StringSession
from telethon import TelegramClient
from opentele2.td import TDesktop
from opentele2.api import CreateNewSession

from app.core.config import settings
from app.core.security import safe_extract_zip, encrypt_session_string

async def import_session_file(session_path: Path, proxy: Optional[Dict[str, Any]] = None) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
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
            return False, "Сессия не авторизована или отозвана в Telegram", None

        user = await client.get_me()
        session_string = StringSession.save(client.session)
        encrypted_token = encrypt_session_string(session_string)

        user_info = {
            "phone": getattr(user, "phone", None) or session_path.stem,
            "first_name": getattr(user, "first_name", None),
            "last_name": getattr(user, "last_name", None),
            "username": getattr(user, "username", None),
            "session_encrypted": encrypted_token
        }
        return True, "Успешно", user_info
    except Exception as exc:
        return False, f"Ошибка чтения сессии: {exc}", None
    finally:
        await client.disconnect()

async def convert_tdata_archive(
    zip_path: Path,
    password: Optional[str] = None,
    proxy: Optional[Dict[str, Any]] = None
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
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
            await telethon_client.connect()

            if not await telethon_client.is_user_authorized():
                return False, "Не удалось авторизовать новую сессию из TData", None

            user = await telethon_client.get_me()
            session_string = StringSession.save(telethon_client.session)
            encrypted_token = encrypt_session_string(session_string)

            account_info = {
                "phone": getattr(user, "phone", None) or f"tdata_{user.id}",
                "first_name": getattr(user, "first_name", None),
                "last_name": getattr(user, "last_name", None),
                "username": getattr(user, "username", None),
                "session_encrypted": encrypted_token
            }
            return True, "Успешно", account_info
        finally:
            await telethon_client.disconnect()
    except Exception as exc:
        return False, f"Ошибка конвертации TData: {exc}", None
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
