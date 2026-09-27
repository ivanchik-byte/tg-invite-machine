from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from sqlalchemy import select, func

from app.core.database import async_session_factory
from app.models.models import Account, Proxy, AudienceMember
from app.bot.keyboards import main_menu_keyboard
from app.core.utils import safe_edit_text

menu_router = Router()

@menu_router.message(CommandStart())
@menu_router.message(Command("menu"))
async def handle_start(message: Message):
    text = (
        "Панель управления инвайтером и парсером Telegram.\n"
        "Выберите нужный раздел в меню ниже:"
    )
    await message.answer(text, reply_markup=main_menu_keyboard())

@menu_router.callback_query(F.data == "nav_main")
async def callback_nav_main(callback: CallbackQuery):
    text = (
        "Панель управления инвайтером и парсером Telegram.\n"
        "Выберите нужный раздел в меню ниже:"
    )
    await safe_edit_text(callback.message, text, reply_markup=main_menu_keyboard())
    await callback.answer()

@menu_router.callback_query(F.data == "nav_stats")
async def callback_nav_stats(callback: CallbackQuery):
    async with async_session_factory() as session:
        total_accounts = (await session.execute(select(func.count(Account.id)))).scalar_one()
        active_accounts = (await session.execute(select(func.count(Account.id)).where(Account.status == "active"))).scalar_one()
        cooldown_accounts = (await session.execute(select(func.count(Account.id)).where(Account.status == "cooldown"))).scalar_one()

        total_proxies = (await session.execute(select(func.count(Proxy.id)))).scalar_one()
        active_proxies = (await session.execute(select(func.count(Proxy.id)).where(Proxy.is_active == True))).scalar_one()

        pending_users = (await session.execute(select(func.count(AudienceMember.id)).where(AudienceMember.status == "pending"))).scalar_one()
        deferred_users = (await session.execute(select(func.count(AudienceMember.id)).where(AudienceMember.status == "deferred"))).scalar_one()
        invited_users = (await session.execute(select(func.count(AudienceMember.id)).where(AudienceMember.status == "invited"))).scalar_one()
        restricted_users = (await session.execute(select(func.count(AudienceMember.id)).where(AudienceMember.status == "restricted"))).scalar_one()

    text = (
        "Сводная статистика системы:\n\n"
        f"Аккаунты в пуле: {total_accounts} (активно: {active_accounts}, в отлежке: {cooldown_accounts})\n"
        f"Прокси в пуле: {total_proxies} (доступно: {active_proxies})\n\n"
        f"База пользователей:\n"
        f"Ожидают инвайта: {pending_users}\n"
        f"Отложено (флуд): {deferred_users}\n"
        f"Успешно приглашено: {invited_users}\n"
        f"Приватные профили: {restricted_users}\n"
    )
    await safe_edit_text(callback.message, text, reply_markup=main_menu_keyboard())
    await callback.answer()

