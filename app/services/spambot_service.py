import asyncio
from typing import Tuple
from telethon import TelegramClient, errors
from app.models.models import Account
from app.telegram.client_factory import get_telethon_client

OK_PHRASES = (
    "свободен от каких-либо ограничений",
    "в данный момент у вашего аккаунта нет ограничений",
    "никаких ограничений на ваш аккаунт нет",
    "good news, no limits are currently applied",
    "free as a bird"
)

LIMITED_PHRASES = (
    "на ваш аккаунт наложены ограничения",
    "ваш аккаунт ограничен",
    "you are limited",
    "limitations",
    "sending too many messages",
    "temporary limitation",
    "limited for",
    "account is limited",
)

def classify_spambot_reply(reply_text: str) -> Tuple[str, str]:
    lowered = reply_text.lower()
    for phrase in OK_PHRASES:
        if phrase in lowered:
            return "active", "Ограничений нет"

    for phrase in LIMITED_PHRASES:
        if phrase in lowered:
            return "spambot", "Наложен спамблок"

    return "unknown", reply_text[:120].strip() or "Нет ответа"

async def check_account_spambot(account: Account) -> Tuple[str, str]:
    assigned_proxy = getattr(account, "proxy", None)
    client: TelegramClient = get_telethon_client(account, proxy=assigned_proxy)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return "banned", "Сессия отозвана"

        async with client.conversation("@SpamBot", timeout=8) as dialog:
            await dialog.send_message("/start")
            response = await dialog.get_response()
            text = response.raw_text or ""
            status, description = classify_spambot_reply(text)
            return status, description
    except asyncio.TimeoutError:
        return "unknown", "Таймаут ответа @SpamBot"
    except errors.FloodWaitError as flood:
        return "unknown", f"Лимит Telegram, повторите через {flood.seconds} сек"
    except (errors.AuthKeyDuplicatedError, errors.AuthKeyUnregisteredError):
        return "error", "Сессия используется в другом месте или отозвана"
    except Exception as exc:
        return "error", str(exc)
    finally:
        await client.disconnect()
