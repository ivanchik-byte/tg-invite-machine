from html import escape as quote_html
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import async_session_factory
from app.core.settings_service import get_daily_invite_limit, get_speed_profile
from app.models.models import Account, Proxy, AudienceMember, InviteTask

from app.bot.keyboards import main_menu_keyboard
from app.core.utils import safe_edit_text
from app.services.task_manager import invite_task_manager
from app.services.account_service import auto_recover_cooldowns

menu_router = Router()


def render_progress_bar(current: int, total: int, length: int = 10) -> str:
    if total <= 0:
        return f"[{'░' * length}] 0%"
    percent = min(1.0, max(0.0, current / total))
    filled_len = int(round(length * percent))
    bar = "█" * filled_len + "░" * (length - filled_len)
    return f"[{bar}] {int(percent * 100)}%"


async def build_main_dashboard_text() -> str:
    async with async_session_factory() as session:
        await auto_recover_cooldowns(session)
        total_accounts = (await session.execute(select(func.count(Account.id)))).scalar_one()
        active_accounts = (await session.execute(
            select(func.count(Account.id)).where(Account.is_active == True, Account.status == "active")
        )).scalar_one()
        cooldown_accounts = (await session.execute(
            select(func.count(Account.id)).where(Account.status == "cooldown")
        )).scalar_one()
        banned_accounts = (await session.execute(
            select(func.count(Account.id)).where(Account.status == "banned")
        )).scalar_one()

        total_proxies = (await session.execute(select(func.count(Proxy.id)))).scalar_one()
        active_proxies = (await session.execute(
            select(func.count(Proxy.id)).where(Proxy.is_active == True)
        )).scalar_one()

        pending_users = (await session.execute(
            select(func.count(AudienceMember.id)).where(AudienceMember.status == "pending")
        )).scalar_one()
        invited_users = (await session.execute(
            select(func.count(AudienceMember.id)).where(AudienceMember.status == "invited")
        )).scalar_one()
        restricted_users = (await session.execute(
            select(func.count(AudienceMember.id)).where(AudienceMember.status == "restricted")
        )).scalar_one()

        is_running = invite_task_manager.is_running()
        active_task_id = invite_task_manager.get_active_task_id()
        active_task = None
        if active_task_id:
            active_task = (await session.execute(
                select(InviteTask)
                .options(selectinload(InviteTask.target_group))
                .where(InviteTask.id == active_task_id)
            )).scalar_one_or_none()

    orch = invite_task_manager.active_orchestrator
    is_paused = getattr(orch, "is_paused", False)

    if is_running and is_paused:
        status_tag = "<code>[ PAUSED ] Инвайтинг на паузе</code>"
    elif is_running:
        status_tag = "<code>[ RUNNING ] Выполняется инвайтинг</code>"
    else:
        status_tag = "<code>[ IDLE ] Ожидание запуска</code>"

    header = (
        "<b>TG-INVITE-MACHINE</b> <code>v0.1.2</code> | <b>Консоль управления</b>\n"
        "────────────────────────\n"
        f"<b>Статус системы:</b> {status_tag}\n\n"
    )


    task_block = ""
    if is_running and active_task:
        target_name = active_task.target_group.target_link if active_task.target_group else f"Группа #{active_task.target_group_id}"
        total_goal = active_task.max_invites or active_task.total_targets or 1
        bar = render_progress_bar(active_task.success_count, total_goal)
        task_block = (
            "<b>Активная задача:</b>\n"
            f"• <b>Цель:</b> <code>{quote_html(target_name)}</code>\n"
            f"• <b>Прогресс:</b> <code>{bar}</code> ({active_task.success_count}/{total_goal})\n"
            f"• <b>Флуд-паузы:</b> <code>{active_task.flood_waits_count}</code>\n\n"
        )

    current_speed = await get_speed_profile()
    speed_label = current_speed.replace("custom:", "свой: ") if current_speed.startswith("custom:") else current_speed
    proxy_mode = "Строгий Zero-Leak" if settings.REQUIRE_STRICT_PROXIES else "Прямой (без прокси)"

    daily_limit = await get_daily_invite_limit()
    pools_block = (
        "<b>Ресурсный пул:</b>\n"
        f"• <b>Аккаунты:</b> <code>{total_accounts}</code> всего "
        f"<code>({active_accounts} активны, {cooldown_accounts} отлежка, {banned_accounts} бан)</code>\n"
        f"• <b>Прокси:</b> <code>{active_proxies} / {total_proxies}</code> онлайн\n"
        f"• <b>База контактов:</b> <code>{pending_users:,}</code> в очереди | <code>{invited_users:,}</code> инвайтов | <code>{restricted_users:,}</code> приватных\n\n"
        "<b>Конфигурация воркера:</b>\n"
        f"• <b>Профиль скорости:</b> <code>{speed_label}</code>\n"
        f"• <b>Сетевой контур:</b> <code>{proxy_mode}</code>\n"
        f"• <b>Суточный лимит:</b> <code>{daily_limit}</code> успешно приглашенных/сессию\n\n"
    )


    if is_running:
        footer = "<blockquote>Инвайтинг выполняется в фоновом режиме. Выберите действие ниже.</blockquote>"
    elif total_accounts == 0:
        footer = "<blockquote>Пул аккаунтов пуст. Загрузите .session или .zip архив с TData для начала работы.</blockquote>"
    else:
        footer = "<blockquote>Система готова к работе. Выберите нужный модуль в меню ниже:</blockquote>"

    return f"{header}{task_block}{pools_block}{footer}"


async def build_stats_text() -> str:
    async with async_session_factory() as session:
        await auto_recover_cooldowns(session)
        total_accounts = (await session.execute(select(func.count(Account.id)))).scalar_one()
        active_accounts = (await session.execute(
            select(func.count(Account.id)).where(Account.is_active == True, Account.status == "active")
        )).scalar_one()
        cooldown_accounts = (await session.execute(
            select(func.count(Account.id)).where(Account.status == "cooldown")
        )).scalar_one()
        banned_accounts = (await session.execute(
            select(func.count(Account.id)).where(Account.status == "banned")
        )).scalar_one()

        total_proxies = (await session.execute(select(func.count(Proxy.id)))).scalar_one()
        active_proxies = (await session.execute(
            select(func.count(Proxy.id)).where(Proxy.is_active == True)
        )).scalar_one()

        pending_users = (await session.execute(
            select(func.count(AudienceMember.id)).where(AudienceMember.status == "pending")
        )).scalar_one()
        invited_users = (await session.execute(
            select(func.count(AudienceMember.id)).where(AudienceMember.status == "invited")
        )).scalar_one()
        restricted_users = (await session.execute(
            select(func.count(AudienceMember.id)).where(AudienceMember.status == "restricted")
        )).scalar_one()
        deferred_users = (await session.execute(
            select(func.count(AudienceMember.id)).where(AudienceMember.status == "deferred")
        )).scalar_one()

    is_running = invite_task_manager.is_running()
    orch = invite_task_manager.active_orchestrator
    is_paused = getattr(orch, "is_paused", False)
    if is_running and is_paused:
        worker_status = "[ PAUSED ]"
    elif is_running:
        worker_status = "[ RUNNING ]"
    else:
        worker_status = "[ IDLE ]"

    current_speed = await get_speed_profile()
    speed_label = current_speed.replace("custom:", "свой: ") if current_speed.startswith("custom:") else current_speed
    total_audience = pending_users + invited_users + restricted_users + deferred_users

    text = (
        "<b>TG-INVITE-MACHINE | Системная телеметрия</b>\n"
        "────────────────────────\n"
        "<pre><code>МЕТРИКА               ЗНАЧЕНИЕ\n"
        "───────────────────────────────\n"
        f"Пул сессий:           {total_accounts} шт.\n"
        f"├─ Активные:          {active_accounts}\n"
        f"├─ В отлежке:         {cooldown_accounts}\n"
        f"└─ Заблокированы:     {banned_accounts}\n"
        "───────────────────────────────\n"
        f"Сетевой контур:       {total_proxies} шт.\n"
        f"├─ Доступны:          {active_proxies}\n"
        f"└─ Ошибки/Офлайн:     {total_proxies - active_proxies}\n"
        "───────────────────────────────\n"
        f"База аудитории:       {total_audience} чел.\n"
        f"├─ В очереди:         {pending_users}\n"
        f"├─ Приглашено:        {invited_users}\n"
        f"├─ Приватные профили: {restricted_users}\n"
        f"└─ Отложено (флуд):   {deferred_users}\n"
        "───────────────────────────────\n"
        f"Статус воркера:       {worker_status}\n"
        f"Профиль задержки:     {speed_label}</code></pre>\n\n"
        "<blockquote>Для выгрузки списков используйте раздел «Аккаунты» (.xlsx) или меню парсера аудитории.</blockquote>"
    )
    return text


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
