from html import escape as quote_html

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import async_session_factory
from app.core.settings_service import get_daily_invite_limit, get_speed_profile
from app.models.models import Account, AudienceMember, InviteTask, Proxy
from app.services.account_service import auto_recover_cooldowns
from app.services.task_manager import invite_task_manager


def render_progress_bar(current: int, total: int, length: int = 10) -> str:
    if total <= 0:
        return f"[{'░' * length}] 0%"
    percent = min(1.0, max(0.0, current / total))
    filled = int(round(length * percent))
    return f"[{'█' * filled}{'░' * (length - filled)}] {int(percent * 100)}%"


async def pool_counts(session: AsyncSession) -> dict:
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
    return {
        "total_accounts": total_accounts,
        "active_accounts": active_accounts,
        "cooldown_accounts": cooldown_accounts,
        "banned_accounts": banned_accounts,
        "total_proxies": total_proxies,
        "active_proxies": active_proxies,
        "pending_users": pending_users,
        "invited_users": invited_users,
        "restricted_users": restricted_users,
        "deferred_users": deferred_users,
    }


def worker_tag() -> str:
    running = invite_task_manager.is_running()
    paused = getattr(invite_task_manager.active_orchestrator, "is_paused", False)
    if running and paused:
        return "[ PAUSED ]"
    if running:
        return "[ RUNNING ]"
    return "[ IDLE ]"


async def build_main_dashboard_text() -> str:
    async with async_session_factory() as session:
        await auto_recover_cooldowns(session)
        counts = await pool_counts(session)
        active_task = None
        active_task_id = invite_task_manager.get_active_task_id()
        if active_task_id:
            active_task = (await session.execute(
                select(InviteTask)
                .options(selectinload(InviteTask.target_group))
                .where(InviteTask.id == active_task_id)
            )).scalar_one_or_none()

    tag = worker_tag()
    if tag == "[ PAUSED ]":
        status_tag = "<code>[ PAUSED ] Инвайтинг на паузе</code>"
    elif tag == "[ RUNNING ]":
        status_tag = "<code>[ RUNNING ] Выполняется инвайтинг</code>"
    else:
        status_tag = "<code>[ IDLE ] Ожидание запуска</code>"
    header = (
        "<b>TG-INVITE-MACHINE</b> <code>v0.2.0</code> | <b>Консоль управления</b>\n"
        "────────────────────────\n"
        f"<b>Статус системы:</b> {status_tag}\n\n"
    )
    task_block = ""
    if invite_task_manager.is_running() and active_task:
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
        f"• <b>Аккаунты:</b> <code>{counts['total_accounts']}</code> всего "
        f"<code>({counts['active_accounts']} активны, {counts['cooldown_accounts']} отлежка, {counts['banned_accounts']} бан)</code>\n"
        f"• <b>Прокси:</b> <code>{counts['active_proxies']} / {counts['total_proxies']}</code> онлайн\n"
        f"• <b>База контактов:</b> <code>{counts['pending_users']:,}</code> в очереди | <code>{counts['invited_users']:,}</code> инвайтов | <code>{counts['restricted_users']:,}</code> приватных\n\n"
        "<b>Конфигурация воркера:</b>\n"
        f"• <b>Профиль скорости:</b> <code>{speed_label}</code>\n"
        f"• <b>Сетевой контур:</b> <code>{proxy_mode}</code>\n"
        f"• <b>Суточный лимит:</b> <code>{daily_limit}</code> успешно приглашенных/сессию\n\n"
    )
    if invite_task_manager.is_running():
        footer = "<blockquote>Инвайтинг выполняется в фоновом режиме. Выберите действие ниже.</blockquote>"
    elif counts["total_accounts"] == 0:
        footer = "<blockquote>Пул аккаунтов пуст. Загрузите .session или .zip архив с TData для начала работы.</blockquote>"
    else:
        footer = "<blockquote>Система готова к работе. Выберите нужный модуль в меню ниже:</blockquote>"
    return f"{header}{task_block}{pools_block}{footer}"


async def build_stats_text() -> str:
    async with async_session_factory() as session:
        await auto_recover_cooldowns(session)
        counts = await pool_counts(session)
    tag = worker_tag()
    current_speed = await get_speed_profile()
    speed_label = current_speed.replace("custom:", "свой: ") if current_speed.startswith("custom:") else current_speed
    total_audience = counts["pending_users"] + counts["invited_users"] + counts["restricted_users"] + counts["deferred_users"]
    return (
        "<b>TG-INVITE-MACHINE | Системная телеметрия</b>\n"
        "────────────────────────\n"
        "<pre><code>МЕТРИКА               ЗНАЧЕНИЕ\n"
        "───────────────────────────────\n"
        f"Пул сессий:           {counts['total_accounts']} шт.\n"
        f"├─ Активные:          {counts['active_accounts']}\n"
        f"├─ В отлежке:         {counts['cooldown_accounts']}\n"
        f"└─ Заблокированы:     {counts['banned_accounts']}\n"
        "───────────────────────────────\n"
        f"Сетевой контур:       {counts['total_proxies']} шт.\n"
        f"├─ Доступны:          {counts['active_proxies']}\n"
        f"└─ Ошибки/Офлайн:     {counts['total_proxies'] - counts['active_proxies']}\n"
        "───────────────────────────────\n"
        f"База аудитории:       {total_audience} чел.\n"
        f"├─ В очереди:         {counts['pending_users']}\n"
        f"├─ Приглашено:        {counts['invited_users']}\n"
        f"├─ Приватные профили: {counts['restricted_users']}\n"
        f"└─ Отложено (флуд):   {counts['deferred_users']}\n"
        "───────────────────────────────\n"
        f"Статус воркера:       {tag}\n"
        f"Профиль задержки:     {speed_label}</code></pre>\n\n"
        "<blockquote>Для выгрузки списков используйте раздел «Аккаунты» (.xlsx) или меню парсера аудитории.</blockquote>"
    )
