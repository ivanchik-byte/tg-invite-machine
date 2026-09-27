from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
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

@menu_router.message(Command("help"))
async def handle_help(message: Message):
    help_text = (
        "Справка по управлению tg-invite-machine:\n\n"
        "1. Аккаунты:\n"
        "* Загрузка: отправьте .session файл или .zip архив с папкой tdata документом в чат.\n"
        "* 2FA: если на аккаунте установлен облачный пароль, бот запросит его и автоматически удалит пароль из чата для безопасности.\n"
        "* Изоляция сессий: при импорте TData создается отдельная авторизованная сессия через QR-login, что исключает конфликт с Telegram Desktop на ПК.\n"
        "* Проверка: быстрая проверка валидности пула и опрос @SpamBot в один клик.\n\n"
        "2. Прокси:\n"
        "* Поддерживаются форматы SOCKS5, SOCKS4, HTTP (ip:port:user:pass или host:port).\n"
        "* Добавление списком из нескольких строк.\n\n"
        "3. Сбор аудитории:\n"
        "* Сбор всех участников супергруппы по алфавиту.\n"
        "* Фильтрация активных участников по истории сообщений за N дней.\n\n"
        "4. Инвайтинг:\n"
        "* Настройка лимита инвайтов (быстрые кнопки или произвольное число).\n"
        "* Профили скорости: Осторожный (40-85 сек), Стандартный (25-50 сек), Быстрый (15-30 сек) или Свой диапазон.\n"
        "* Защита: тримодальное распределение задержек, автоматическая отлежка аккаунтов при FloodWait и Circuit Breaker при спамблоке.\n\n"
        "Команды бота:\n"
        "/start - Главное меню управления\n"
        "/menu - Открыть главное меню\n"
        "/stats - Сводная статистика системы\n"
        "/help - Справка и руководство\n"
        "/cancel - Отмена текущего действия или ввода\n\n"
        "Связь с разработчиком:\n"
        "Telegram PM: https://t.me/ivanchikbyte\n"
        "Канал проекта: https://t.me/ivanchik_byte"
    )
    await message.answer(help_text, reply_markup=main_menu_keyboard(), disable_web_page_preview=True)

@menu_router.message(Command("cancel"))
async def handle_cancel(message: Message, state: FSMContext | None = None):
    if state is not None:
        await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu_keyboard())

@menu_router.message(Command("stats"))
async def handle_stats_command(message: Message):
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
    await message.answer(text, reply_markup=main_menu_keyboard())

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

