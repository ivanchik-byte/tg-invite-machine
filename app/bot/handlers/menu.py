from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from app.bot.keyboards import main_menu_keyboard
from app.bot.dashboard import build_main_dashboard_text, build_stats_text, render_progress_bar
from app.core.utils import safe_edit_text

__all__ = ["build_main_dashboard_text", "build_stats_text", "render_progress_bar"]

menu_router = Router()


@menu_router.message(CommandStart())
@menu_router.message(Command("menu"))
async def handle_start(message: Message):
    text = await build_main_dashboard_text()
    await message.answer(text, reply_markup=main_menu_keyboard())


@menu_router.callback_query(F.data == "nav_main")
async def callback_nav_main(callback: CallbackQuery):
    text = await build_main_dashboard_text()
    await safe_edit_text(callback.message, text, reply_markup=main_menu_keyboard())
    await callback.answer()


@menu_router.callback_query(F.data == "nav_main_refresh")
async def callback_nav_main_refresh(callback: CallbackQuery):
    text = await build_main_dashboard_text()
    await safe_edit_text(callback.message, text, reply_markup=main_menu_keyboard())
    await callback.answer("Дашборд обновлен.")


@menu_router.message(Command("help"))
async def handle_help(message: Message):
    help_text = (
        "<b>TG-INVITE-MACHINE | Справка по управлению</b>\n"
        "────────────────────────\n"
        "Распределенная система сбора аудитории и инвайтинга в супергруппы Telegram через MTProto API.\n\n"
        "<blockquote expandable><b>1. Управление сессиями (Аккаунты)</b>\n"
        "• <b>Загрузка:</b> отправьте в чат файл <code>.session</code> (Telethon) или <code>.zip</code> архив с папкой <code>tdata</code>.\n"
        "• <b>Безопасность 2FA:</b> при наличии облачного пароля бот запросит его и автоматически удалит сообщение из чата для безопасности.\n"
        "• <b>Изоляция сессий:</b> при импорте TData создается изолированная сессия через QR-login без сброса Telegram Desktop на ПК.\n"
        "• <b>Верификация:</b> быстрая проверка валидности пула и опрос @SpamBot в один клик.</blockquote>\n\n"
        "<blockquote expandable><b>2. Сетевой контур (Прокси)</b>\n"
        "• <b>Протоколы:</b> SOCKS5, HTTP, SOCKS4 (с логином и паролем или с авторизацией по IP).\n"
        "• <b>Формат загрузки:</b> списком построчно в виде <code>ip:port:user:pass</code> или <code>host:port</code>.\n"
        "• <b>Безопасность:</b> изоляция каждой сессии на отдельном прокси для исключения взаимных блокировок.</blockquote>\n\n"
        "<blockquote expandable><b>3. Сбор аудитории (Парсер)</b>\n"
        "• <b>Полный сбор (Алфавитный):</b> поиск участников супергруппы через перебор префиксов (обход лимита 10,000).\n"
        "• <b>Активные авторы:</b> фильтрация только тех, кто писал в чат за последние N дней (отсеивает ботов и неактивные аккаунты).</blockquote>\n\n"
        "<blockquote expandable><b>4. Защита от банов (Инвайтер)</b>\n"
        "• <b>Тримодальные задержки:</b> комбинирование коротких, средних и глубоких пауз для точной эмуляции человека.\n"
        "• <b>Circuit Breaker:</b> при получении PeerFlood аккаунт переводится в отлежку, а кампания продолжается на резервных сессиях.</blockquote>\n\n"
        "<b>Команды управления:</b>\n"
        "<code>/start</code> | <code>/menu</code> - Главный дашборд системы\n"
        "<code>/stats</code> - Расширенная телеметрия и аналитика\n"
        "<code>/help</code> - Справка по управлению\n"
        "<code>/cancel</code> - Прерывание текущей операции ввода\n\n"
        "<b>Связь с разработчиком:</b>\n"
        "• Telegram PM: <a href=\"https://t.me/ivanchikbyte\">@ivanchikbyte</a>\n"
        "• Канал проекта: <a href=\"https://t.me/ivanchik_byte\">@ivanchik_byte</a>"
    )
    await message.answer(help_text, reply_markup=main_menu_keyboard(), disable_web_page_preview=True)


@menu_router.message(Command("cancel"))
async def handle_cancel(message: Message, state: FSMContext | None = None):
    if state is not None:
        await state.clear()
    await message.answer("Действие отменено. Возврат в главное меню.", reply_markup=main_menu_keyboard())


@menu_router.message(Command("stats"))
async def handle_stats_command(message: Message):
    text = await build_stats_text()
    await message.answer(text, reply_markup=main_menu_keyboard())


@menu_router.callback_query(F.data == "nav_stats")
async def callback_nav_stats(callback: CallbackQuery):
    text = await build_stats_text()
    await safe_edit_text(callback.message, text, reply_markup=main_menu_keyboard())
    await callback.answer()
