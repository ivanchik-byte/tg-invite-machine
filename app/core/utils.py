from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, InlineKeyboardMarkup

def normalize_chat_identifier(link_or_username: str) -> str:
    clean = link_or_username.strip()
    if clean.startswith("https://t.me/"):
        clean = clean.replace("https://t.me/", "")
    elif clean.startswith("http://t.me/"):
        clean = clean.replace("http://t.me/", "")
    elif clean.startswith("t.me/"):
        clean = clean.replace("t.me/", "")
    if clean.startswith("+") or clean.startswith("joinchat/"):
        # Private invite links are case-sensitive
        return clean
    if clean.startswith("@"):
        clean = clean[1:]
    clean = clean.split("/")[0].split("?")[0].strip().lower()
    return clean

async def safe_edit_text(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
        return True
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return False
        raise exc
