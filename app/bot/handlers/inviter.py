import asyncio
import logging
import time
from typing import Optional
from datetime import datetime, timezone
from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from html import escape as quote_html
from sqlalchemy import select, func, update
from sqlalchemy.orm import selectinload
from telethon.tl.types import Channel, Chat
from telethon.tl.functions.messages import MigrateChatRequest

from app.core.config import settings
from app.core.database import async_session_factory
from app.services.account_service import auto_recover_cooldowns
from app.models.models import Account, AudienceMember, TargetGroup, InviteTask
from app.core.settings_service import (
    get_daily_invite_limit,
    set_daily_invite_limit,
    get_excluded_worker_ids,
    get_privacy_blacklist_enabled,
    set_privacy_blacklist_enabled,
    get_recent_only_enabled,
    set_recent_only_enabled,
    get_speed_profile,
    set_speed_profile,
    get_default_concurrency_mode,
)
from app.bot.states import InviterState
from app.bot.keyboards import (
    inviter_menu_keyboard,
    speed_profile_keyboard,
    inviter_config_keyboard,
    back_keyboard,
    migrate_confirm_keyboard,
    daily_limit_keyboard,
    main_menu_keyboard,
)
from app.bot.dashboard import build_main_dashboard_text
from app.services.inviter_service import InviterOrchestrator
from app.services.task_manager import invite_task_manager
from app.telegram.client_factory import get_telethon_client
from app.core.utils import normalize_chat_identifier, safe_edit_text
from app.services.proxy_service import auto_assign_proxies

logger = logging.getLogger("tg_invite_machine")

inviter_router = Router()


VALID_SPEED_PROFILES = {"cautious", "normal", "fast"}

@inviter_router.callback_query(F.data == "nav_inviter")
async def callback_nav_inviter(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    is_running = invite_task_manager.is_running()

    async with async_session_factory() as session:
        await auto_recover_cooldowns(session)
        pending_count = (await session.execute(
            select(func.count(AudienceMember.id)).where(AudienceMember.status == "pending")
        )).scalar_one()
        active_accounts = (await session.execute(
            select(func.count(Account.id)).where(Account.is_active == True, Account.status == "active")
        )).scalar_one()
        cooldown_accounts = (await session.execute(
            select(func.count(Account.id)).where(Account.status == "cooldown")
        )).scalar_one()
        paused_task = (await session.execute(
            select(InviteTask).where(InviteTask.status == "paused").order_by(InviteTask.id.desc()).limit(1)
        )).scalars().first()
        paused_task_id = paused_task.id if paused_task else None

    orch = invite_task_manager.active_orchestrator
    is_paused = getattr(orch, "is_paused", False)
    if is_running and is_paused:
        status_tag = "<code>[ PAUSED ] Инвайтинг на паузе</code>"
    elif is_running:
        status_tag = "<code>[ RUNNING ] Выполняется инвайтинг</code>"
    else:
        status_tag = "<code>[ IDLE ] Остановлен</code>"

    daily_limit = await get_daily_invite_limit()
    blacklist_enabled = await get_privacy_blacklist_enabled()
    recent_only = await get_recent_only_enabled()
    current_speed = await get_speed_profile()
    speed_label = current_speed.replace("custom:", "свой: ") if current_speed.startswith("custom:") else current_speed
    bl_label = "ВКЛ (пропуск закрытых)" if blacklist_enabled else "ВЫКЛ (пробовать всех)"
    recent_label = "ВКЛ (только недавно в сети)" if recent_only else "ВЫКЛ (все собранные)"

    text = (
        "<b>TG-INVITE-MACHINE | Центр управления инвайтингом</b>\n"
        "────────────────────────\n"
        f"<b>Статус воркера:</b> {status_tag}\n\n"
        "<b>Параметры очереди:</b>\n"
        f"• <b>Пользователей в очереди:</b> <code>{pending_count}</code> чел.\n"
        f"• <b>Готовых сессий:</b> <code>{active_accounts}</code> шт. (в отлежке: <code>{cooldown_accounts}</code>)\n"
        f"• <b>Суточный лимит:</b> <code>{daily_limit}</code> успешно приглашенных на акк\n"
        f"• <b>Блэклист приватности:</b> <code>{bl_label}</code>\n"
        f"• <b>Фильтр недавних:</b> <code>{recent_label}</code>\n"
        f"• <b>Профиль скорости:</b> <code>{speed_label}</code>\n"
        f"• <b>Алгоритм пауз:</b> <code>Тримодальное распределение</code>\n\n"
        "<blockquote>При запуске бот проверит статус группы и запустит распределенный цикл с автоматической ротацией сессий при FloodWait.</blockquote>"
    )
    await safe_edit_text(
        callback.message,
        text,
        reply_markup=inviter_menu_keyboard(
            task_running=is_running,
            is_paused=is_paused,
            daily_limit=daily_limit,
            privacy_blacklist=blacklist_enabled,
            recent_only=recent_only,
            paused_task_id=paused_task_id,
        )
    )
    await callback.answer()


@inviter_router.callback_query(F.data == "invite_toggle_blacklist")
async def callback_invite_toggle_blacklist(callback: CallbackQuery, state: FSMContext):
    current = await get_privacy_blacklist_enabled()
    new_val = not current
    await set_privacy_blacklist_enabled(new_val)
    if not new_val:
        async with async_session_factory() as session:
            await session.execute(
                update(AudienceMember)
                .where(
                    AudienceMember.status == "restricted",
                    AudienceMember.reason == "Исключен блэклистом приватности"
                )
                .values(status="pending", reason=None)
            )
            await session.commit()
    status_str = "включен (пропуск закрытых профилей)" if new_val else "выключен"
    await callback.answer(f"Блэклист приватности {status_str}")
    await callback_nav_inviter(callback, state)


@inviter_router.callback_query(F.data == "invite_toggle_recent")
async def callback_invite_toggle_recent(callback: CallbackQuery, state: FSMContext):
    current = await get_recent_only_enabled()
    new_val = not current
    await set_recent_only_enabled(new_val)
    status_str = "включен (только недавно в сети)" if new_val else "выключен (все собранные)"
    await callback.answer(f"Фильтр недавних {status_str}")
    await callback_nav_inviter(callback, state)


@inviter_router.callback_query(F.data == "invite_speed")
async def callback_invite_speed(callback: CallbackQuery):
    curr_profile = await get_speed_profile()
    text = (
        "Выберите профиль скорости инвайта:\n\n"
        "Осторожный (50-110 сек): минимальный риск, для свежих аккаунтов.\n"
        "Обычный (35-75 сек): рекомендуемый баланс скорости и надежности.\n"
        "Быстрый (17-37 сек): повышенная скорость, для прогретых аккаунтов.\n"
        "Свой интервал: ручная настройка диапазона задержки."
    )
    await safe_edit_text(callback.message, text, reply_markup=speed_profile_keyboard(curr_profile))
    await callback.answer()

@inviter_router.callback_query(F.data.startswith("set_speed_"))
async def callback_set_speed(callback: CallbackQuery, state: FSMContext):
    profile = callback.data.replace("set_speed_", "")
    if profile == "custom":
        await state.set_state(InviterState.waiting_for_custom_delay)
        await state.update_data(source="main_menu")
        await callback.message.edit_text(
            "Введите диапазон задержки между инвайтами в секундах (от 0 до 600, например: 0-1 или 40-80):",
            reply_markup=back_keyboard("nav_inviter")
        )
        await callback.answer()
        return

    if profile not in VALID_SPEED_PROFILES:
        await callback.answer("Недопустимый профиль скорости.", show_alert=True)
        return
    await set_speed_profile(profile)
    await callback.answer(f"Установлен профиль: {profile}")
    await callback_nav_inviter(callback, state)

@inviter_router.callback_query(F.data == "invite_daily_limit")
async def callback_invite_daily_limit(callback: CallbackQuery):
    daily_limit = await get_daily_invite_limit()
    text = (
        "<b>TG-INVITE-MACHINE | Суточный лимит инвайтов</b>\n"
        "────────────────────────\n"
        f"• Текущий лимит: <code>{daily_limit}</code> успешно приглашенных в сутки на аккаунт.\n\n"
        "<b>Важно:</b> лимит считает ТОЛЬКО реально добавленных пользователей в группу (успешные инвайты).\n"
        "Ошибки приватности, уже состоящие в группе и временные спамблоки в лимит НЕ входят.\n\n"
        "Выберите новое значение или укажите свое:"
    )
    await safe_edit_text(callback.message, text, reply_markup=daily_limit_keyboard(daily_limit))
    await callback.answer()

@inviter_router.callback_query(F.data.startswith("set_daily_"))
async def callback_set_daily_limit_handler(callback: CallbackQuery, state: FSMContext):
    val_str = callback.data.replace("set_daily_", "")
    if val_str == "custom":
        await state.set_state(InviterState.waiting_for_daily_limit)
        await callback.message.edit_text(
            "Введите суточный лимит успешных инвайтов на один аккаунт (от 1 до 500):\n"
            "<i>Например: 20 или 50</i>\n\n"
            "<i>Для отмены отправьте /cancel.</i>",
            reply_markup=back_keyboard("nav_inviter")
        )
        await callback.answer()
        return

    try:
        new_limit = int(val_str)
        await set_daily_invite_limit(new_limit)
        await callback.answer(f"Суточный лимит установлен: {new_limit} инвайтов/сессию", show_alert=True)
        await callback_nav_inviter(callback, state)
    except Exception as exc:
        await callback.answer(f"Ошибка: {exc}", show_alert=True)

@inviter_router.message(InviterState.waiting_for_daily_limit, F.text)
async def handle_custom_daily_limit(message: Message, state: FSMContext):
    if message.text.strip().startswith("/cancel"):
        await state.clear()
        await message.answer("Отменено.")
        dashboard_text = await build_main_dashboard_text()
        await message.answer(dashboard_text, reply_markup=main_menu_keyboard())
        return

    try:
        limit = int(message.text.strip())
        if limit <= 0 or limit > 500:
            raise ValueError
    except ValueError:
        await message.answer("Пожалуйста, введите положительное число от 1 до 500 (или /cancel):")
        return

    await set_daily_invite_limit(limit)
    await state.clear()
    await message.answer(
        f"Суточный лимит успешно установлен: <code>{limit}</code> успешно приглашенных на аккаунт."
    )
    dashboard_text = await build_main_dashboard_text()
    await message.answer(dashboard_text, reply_markup=main_menu_keyboard())


@inviter_router.callback_query(F.data == "invite_start")
async def callback_invite_start(callback: CallbackQuery, state: FSMContext):
    orch = invite_task_manager.active_orchestrator
    if invite_task_manager.is_running() and orch:
        if orch.is_paused:
            orch.resume()
            await callback.answer("Инвайтинг возобновлен.")
            await callback.message.edit_text("Инвайтинг возобновлен.", reply_markup=inviter_menu_keyboard(task_running=True, is_paused=False))
            return
        await callback.answer("Инвайтер уже запущен.", show_alert=True)
        return

    async with async_session_factory() as session:
        await auto_recover_cooldowns(session)
        await auto_assign_proxies(session)
        await session.commit()

        excluded_ids = await get_excluded_worker_ids()
        worker_filter = [Account.is_active == True, Account.status == "active"]
        if excluded_ids:
            worker_filter.append(~Account.id.in_(excluded_ids))
        accounts_count = (await session.execute(
            select(func.count(Account.id)).where(*worker_filter)
        )).scalar_one()
        pending_members = (await session.execute(
            select(func.count(AudienceMember.id)).where(AudienceMember.status == "pending")
        )).scalar_one()

    if accounts_count == 0:
        await callback.answer("В базе нет активных аккаунтов. Добавьте аккаунты перед запуском.", show_alert=True)
        return

    if pending_members == 0:
        await callback.answer("Очередь пользователей пуста. Сначала выполните сбор аудитории.", show_alert=True)
        return

    await state.set_state(InviterState.waiting_for_target_group)
    text = (
        "Введите ссылку или @username целевого канала или группы, куда приглашать людей:\n"
        "Например: @my_target_community или https://t.me/my_target_community\n\n"
        "Убедитесь, что аккаунты из пула состоят в канале/группе или имеют права на добавление."
    )
    await callback.message.edit_text(text, reply_markup=back_keyboard("nav_inviter"))
    await callback.answer()

@inviter_router.callback_query(F.data == "invite_reset_limits")
async def callback_invite_reset_limits(callback: CallbackQuery, state: FSMContext):
    async with async_session_factory() as session:
        await session.execute(
            update(Account).where(Account.is_active == True, Account.status != "banned").values(
                daily_invites_count=0,
                cooldown_until=None,
                status="active"
            )
        )
        await session.commit()
    await callback.answer("Счетчик инвайтов и отлежка аккаунтов сброшены!", show_alert=True)
    await callback_nav_inviter(callback, state)

TARGET_TYPE_LABELS = {"channel": "канал", "supergroup": "супергруппа", "basic_group": "группа"}


@inviter_router.message(InviterState.waiting_for_target_group, F.text)
async def handle_target_group(message: Message, state: FSMContext, bot: Bot):
    target_link = message.text.strip()
    clean_username = normalize_chat_identifier(target_link)
    status_msg = await message.answer("Проверяю тип цели...")

    target_entity = None
    resolve_error = None
    async with async_session_factory() as session:
        probe = (await session.execute(
            select(Account).options(selectinload(Account.proxy)).where(
                Account.is_active == True, Account.status == "active"
            ).limit(1)
        )).scalars().first()
        if not probe:
            await state.clear()
            await status_msg.edit_text(
                "Нет доступных аккаунтов для проверки цели.",
                reply_markup=back_keyboard("nav_inviter"))
            return

        client = get_telethon_client(probe, proxy=probe.proxy)
        try:
            await client.connect()
            target_entity = await client.get_entity(clean_username)
        except Exception as exc:
            resolve_error = str(exc)
            target_entity = None
        finally:
            await client.disconnect()

    if target_entity is None:
        await state.clear()
        err_hint = f"\nПричина: <code>{quote_html(resolve_error)}</code>" if resolve_error else ""
        await status_msg.edit_text(
            f"Не нашел такой канал или группу. Проверь ссылку и права аккаунтов.{err_hint}",
            reply_markup=back_keyboard("nav_inviter"))
        return
    if isinstance(target_entity, Channel):
        chat_type = "channel" if target_entity.broadcast else "supergroup"
    elif isinstance(target_entity, Chat):
        chat_type = "basic_group"
    else:
        await state.clear()
        await status_msg.edit_text(
            "Это не канал и не группа. Нужна ссылка на канал или группу.",
            reply_markup=back_keyboard("nav_inviter"))
        return

    async with async_session_factory() as session:
        target_group = (await session.execute(
            select(TargetGroup).where(TargetGroup.username == clean_username)
        )).scalars().first()
        if not target_group:
            target_group = TargetGroup(
                title=getattr(target_entity, "title", clean_username),
                username=clean_username,
                tg_id=getattr(target_entity, "id", None),
                access_hash=getattr(target_entity, "access_hash", None),
                chat_type=chat_type,
                is_active=True
            )
            session.add(target_group)
        else:
            target_group.chat_type = chat_type
            target_group.tg_id = getattr(target_entity, "id", None)
            target_group.access_hash = getattr(target_entity, "access_hash", None)
        await session.commit()
        target_group_id = target_group.id

    if chat_type == "basic_group":
        await state.set_state(InviterState.waiting_for_migrate_confirm)
        await state.update_data(target_group_id=target_group_id, target_link=target_link)
        await status_msg.edit_text(
            "Это обычная группа: Telegram API не умеет добавлять в нее участников напрямую. "
            "Могу мигрировать ее в супергруппу (необратимо, ID сменится). Продолжить?",
            reply_markup=migrate_confirm_keyboard())
        return

    curr_speed = await get_speed_profile()
    await _show_pre_launch_config(
        status_msg,
        state,
        target_group_id,
        target_link,
        chat_type,
        selected_limit=20,
        speed_profile=curr_speed
    )


@inviter_router.callback_query(F.data == "migrate_confirm")
async def callback_migrate_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot):
    draft = await state.get_data()
    target_group_id = draft.get("target_group_id")
    target_link = draft.get("target_link", "")
    if not target_group_id:
        await callback.answer("Черновик потерян, введи цель заново.", show_alert=True)
        return

    async with async_session_factory() as session:
        target_group = await session.get(TargetGroup, target_group_id)
        worker = (await session.execute(
            select(Account).options(selectinload(Account.proxy)).where(
                Account.is_active == True, Account.status == "active"
            ).limit(1)
        )).scalars().first()
        if not target_group or not worker:
            await state.clear()
            await callback.message.edit_text(
                "Нечем мигрировать: нет цели или доступных аккаунтов.",
                reply_markup=back_keyboard("nav_inviter"))
            await callback.answer()
            return

        client = get_telethon_client(worker, proxy=worker.proxy)
        try:
            await client.connect()
            migrated = await client(MigrateChatRequest(target_group.tg_id))
            fresh = migrated.chats[0]
            target_group.chat_type = "supergroup"
            target_group.tg_id = fresh.id
            target_group.access_hash = fresh.access_hash
            await session.commit()
            chat_type = "supergroup"
        except Exception as exc:
            await state.clear()
            await callback.message.edit_text(
                f"Не получилось мигрировать: {quote_html(str(exc))}",
                reply_markup=back_keyboard("nav_inviter"))
            await callback.answer()
            return
        finally:
            await client.disconnect()

    await callback.answer("Группа мигрирована в супергруппу.")
    curr_speed = await get_speed_profile()
    await _show_pre_launch_config(
        callback.message,
        state,
        target_group_id,
        target_link,
        chat_type,
        selected_limit=20,
        speed_profile=curr_speed
    )


@inviter_router.callback_query(F.data == "migrate_cancel")
async def callback_migrate_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "Миграция отменена. Задача не создана.",
        reply_markup=back_keyboard("nav_inviter"))
    await callback.answer()


async def _show_pre_launch_config(
    message: Message,
    state: FSMContext,
    target_group_id: int,
    target_link: str,
    chat_type: str,
    selected_limit: Optional[int] = 20,
    speed_profile: Optional[str] = None,
    concurrency_mode: Optional[str] = None
):
    profile = speed_profile or await get_speed_profile()
    draft = await state.get_data()
    mode = concurrency_mode or draft.get("concurrency_mode") or await get_default_concurrency_mode()
    await state.update_data(
        target_group_id=target_group_id,
        target_link=target_link,
        chat_type=chat_type,
        selected_limit=selected_limit,
        speed_profile=profile,
        concurrency_mode=mode
    )
    type_label = TARGET_TYPE_LABELS.get(chat_type, "сообщество")
    limit_text = str(selected_limit) if selected_limit is not None else "Все доступные"

    if profile.startswith("custom:"):
        speed_text = f"Свой интервал ({profile.replace('custom:', '')}с)"
    elif profile == "cautious":
        speed_text = "Осторожный (50-110с)"
    elif profile == "fast":
        speed_text = "Быстрый (17-37с)"
    else:
        speed_text = "Обычный (35-75с)"

    daily_limit = await get_daily_invite_limit()
    recent_only = await get_recent_only_enabled()
    recent_text = "только недавно в сети" if recent_only else "все собранные"

    if mode == "parallel_async":
        mode_text = "Асинхронно (Мульти-воркер)"
    elif mode == "sync_batch":
        mode_text = "Сразу все (Синхронный залп)"
    else:
        mode_text = "По очереди (Карусель)"

    text = (
        "<b>TG-INVITE-MACHINE | Параметры инвайтинга</b>\n"
        "────────────────────────\n"
        f"• <b>Цель:</b> <code>{quote_html(target_link)}</code> ({type_label})\n"
        f"• <b>Лимит на эту задачу:</b> <code>{limit_text}</code> приглашенных\n"
        f"• <b>Суточный лимит сессий:</b> <code>{daily_limit}</code> успешно добавленных на акк\n"
        f"• <b>Режим скорости:</b> <code>{speed_text}</code>\n"
        f"• <b>Режим воркеров:</b> <code>{mode_text}</code>\n"
        f"• <b>Аудитория:</b> <code>{recent_text}</code>\n\n"
        "<i>Лимиты считают только реально добавленных людей, пропуски и приватные профили лимит не тратят.</i>\n\n"
        "Настройте параметры кнопками ниже и запустите задачу:"
    )
    keyboard = inviter_config_keyboard(
        selected_limit=selected_limit,
        current_profile=profile,
        recent_only=recent_only,
        concurrency_mode=mode
    )
    await safe_edit_text(message, text, reply_markup=keyboard)



@inviter_router.callback_query(F.data.startswith("cfg_limit_"))
async def callback_cfg_limit(callback: CallbackQuery, state: FSMContext):
    limit_str = callback.data.replace("cfg_limit_", "")
    data = await state.get_data()
    target_group_id = data.get("target_group_id")
    target_link = data.get("target_link", "")
    chat_type = data.get("chat_type", "supergroup")
    speed_profile = data.get("speed_profile") or await get_speed_profile()

    if limit_str == "custom":
        await state.set_state(InviterState.waiting_for_invite_limit)
        await callback.message.edit_text(
            "Введите число участников для инвайта (от 1 до 5000):\nНапример: 15",
            reply_markup=back_keyboard("cfg_return")
        )
        await callback.answer()
        return

    if limit_str == "all":
        selected_limit = None
    else:
        try:
            selected_limit = int(limit_str)
        except ValueError:
            selected_limit = 20

    await _show_pre_launch_config(
        callback.message,
        state,
        target_group_id,
        target_link,
        chat_type,
        selected_limit=selected_limit,
        speed_profile=speed_profile
    )
    await callback.answer()


@inviter_router.callback_query(F.data.startswith("cfg_speed_"))
async def callback_cfg_speed(callback: CallbackQuery, state: FSMContext):
    speed_val = callback.data.replace("cfg_speed_", "")
    data = await state.get_data()
    target_group_id = data.get("target_group_id")
    target_link = data.get("target_link", "")
    chat_type = data.get("chat_type", "supergroup")
    selected_limit = data.get("selected_limit", 20)

    if speed_val == "custom":
        await state.set_state(InviterState.waiting_for_custom_delay)
        await state.update_data(source="task_config")
        await callback.message.edit_text(
            "Введите диапазон задержки между инвайтами в секундах (от 0 до 600, например: 0-1 или 40-80):",
            reply_markup=back_keyboard("cfg_return")
        )
        await callback.answer()
        return

    await _show_pre_launch_config(
        callback.message,
        state,
        target_group_id,
        target_link,
        chat_type,
        selected_limit=selected_limit,
        speed_profile=speed_val
    )
    await callback.answer()


@inviter_router.callback_query(F.data == "cfg_return")
async def callback_cfg_return(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target_group_id = data.get("target_group_id")
    target_link = data.get("target_link", "")
    chat_type = data.get("chat_type", "supergroup")
    selected_limit = data.get("selected_limit", 20)
    speed_profile = data.get("speed_profile") or await get_speed_profile()
    await _show_pre_launch_config(
        callback.message,
        state,
        target_group_id,
        target_link,
        chat_type,
        selected_limit=selected_limit,
        speed_profile=speed_profile
    )
    await callback.answer()


@inviter_router.callback_query(F.data == "cfg_mode_carousel")
async def callback_cfg_mode_carousel(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await set_recent_only_enabled(True)
    await _show_pre_launch_config(
        callback.message,
        state,
        data.get("target_group_id"),
        data.get("target_link", ""),
        data.get("chat_type", "supergroup"),
        selected_limit=None,
        speed_profile="custom:120:300"
    )
    await callback.answer("Режим Карусель: вся очередь, паузы 2-5 мин, только недавно в сети")


@inviter_router.callback_query(F.data == "cfg_mode_target")
async def callback_cfg_mode_target(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await set_recent_only_enabled(False)
    selected_limit = data.get("selected_limit") or 10
    await _show_pre_launch_config(
        callback.message,
        state,
        data.get("target_group_id"),
        data.get("target_link", ""),
        data.get("chat_type", "supergroup"),
        selected_limit=selected_limit,
        speed_profile=data.get("speed_profile") or await get_speed_profile()
    )
    await callback.answer("Режим Целевой план: остановка ровно на лимите успешных")


@inviter_router.callback_query(F.data.startswith("cfg_dispatch_"))
async def callback_cfg_dispatch(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.replace("cfg_dispatch_", "")
    data = await state.get_data()
    target_group_id = data.get("target_group_id")
    target_link = data.get("target_link", "")
    chat_type = data.get("chat_type", "supergroup")
    selected_limit = data.get("selected_limit", 20)
    speed_profile = data.get("speed_profile") or await get_speed_profile()

    await _show_pre_launch_config(
        callback.message,
        state,
        target_group_id,
        target_link,
        chat_type,
        selected_limit=selected_limit,
        speed_profile=speed_profile,
        concurrency_mode=mode
    )
    labels = {
        "sequential": "Режим: По очереди (Карусель)",
        "parallel_async": "Режим: Асинхронно (Мульти-воркер)",
        "sync_batch": "Режим: Сразу все (Синхронный залп)",
    }
    await callback.answer(labels.get(mode, "Режим воркеров обновлен"))


@inviter_router.callback_query(F.data == "cfg_cancel")
async def callback_cfg_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback_nav_inviter(callback, state)


@inviter_router.message(InviterState.waiting_for_invite_limit, F.text)
async def handle_custom_limit(message: Message, state: FSMContext):
    raw_val = message.text.strip()
    try:
        limit_val = int(raw_val)
        if limit_val <= 0 or limit_val > 5000:
            raise ValueError()
    except ValueError:
        await message.answer(
            "Пожалуйста, введите корректное число от 1 до 5000:\nНапример: 15",
            reply_markup=back_keyboard("cfg_return")
        )
        return

    data = await state.get_data()
    target_group_id = data.get("target_group_id")
    target_link = data.get("target_link", "")
    chat_type = data.get("chat_type", "supergroup")
    speed_profile = data.get("speed_profile") or await get_speed_profile()

    status_msg = await message.answer("Обновление настроек...")
    await _show_pre_launch_config(
        status_msg,
        state,
        target_group_id,
        target_link,
        chat_type,
        selected_limit=limit_val,
        speed_profile=speed_profile
    )


@inviter_router.message(InviterState.waiting_for_custom_delay, F.text)
async def handle_custom_delay(message: Message, state: FSMContext):
    raw_val = message.text.strip().replace(":", "-").replace(" ", "-")
    parts = [p.strip() for p in raw_val.split("-") if p.strip()]
    if len(parts) == 1:
        parts = [parts[0], parts[0]]
    valid = False
    min_pause, max_pause = 0.0, 0.0
    if len(parts) == 2:
        try:
            min_pause = float(parts[0])
            max_pause = float(parts[1])
            if min_pause > max_pause:
                min_pause, max_pause = max_pause, min_pause
            if 0.0 <= min_pause and max_pause <= 600.0:
                valid = True
        except ValueError:
            valid = False

    if not valid:
        await message.answer(
            "<b>[ОШИБКА] Некорректный диапазон</b>\n\n"
            "Пожалуйста, введите диапазон задержки от 0 до 600 секунд (например: 0-1 или 40-80):",
            reply_markup=back_keyboard("cfg_return")
        )
        return

    def fmt(v: float) -> str:
        return str(int(v)) if float(v).is_integer() else str(v)

    custom_profile = f"custom:{fmt(min_pause)}:{fmt(max_pause)}"
    data = await state.get_data()
    source = data.get("source", "task_config")

    warning_text = ""
    if max_pause <= 1.0:
        warning_text = (
            "\n\n<b>[ВНИМАНИЕ] Режим турбо (0-1 сек):</b>\n"
            "Высокий риск блокировки сессий и целевого чата со стороны алгоритмов Telegram."
        )

    if source == "main_menu":
        await set_speed_profile(custom_profile)
        await state.clear()
        await message.answer(
            f"<b>[УСПЕХ] Профиль скорости сохранен</b>\n\n"
            f"• Установлен интервал: <code>{min_pause}-{max_pause} сек</code>"
            f"{warning_text}",
            reply_markup=back_keyboard("nav_inviter")
        )
        return

    target_group_id = data.get("target_group_id")
    target_link = data.get("target_link", "")
    chat_type = data.get("chat_type", "supergroup")
    selected_limit = data.get("selected_limit", 20)

    status_msg = await message.answer("Обновление настроек...")
    await _show_pre_launch_config(
        status_msg,
        state,
        target_group_id,
        target_link,
        chat_type,
        selected_limit=selected_limit,
        speed_profile=custom_profile
    )


@inviter_router.callback_query(F.data == "cfg_launch")
async def callback_cfg_launch(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    target_group_id = data.get("target_group_id")
    target_link = data.get("target_link", "")
    chat_type = data.get("chat_type", "supergroup")
    selected_limit = data.get("selected_limit", 20)
    speed_profile = data.get("speed_profile") or await get_speed_profile()
    concurrency_mode = data.get("concurrency_mode") or await get_default_concurrency_mode()

    if not target_group_id or not target_link:
        await callback.answer("Ошибка: данные задачи устарели. Начните заново.", show_alert=True)
        await callback_nav_inviter(callback, state)
        return

    await _create_and_launch_task(
        target_group_id=target_group_id,
        target_link=target_link,
        chat_type=chat_type,
        selected_limit=selected_limit,
        speed_profile=speed_profile,
        concurrency_mode=concurrency_mode,
        state=state,
        bot=bot,
        status_msg=callback.message
    )
    await callback.answer()


async def _create_and_launch_task(
    target_group_id: int,
    target_link: str,
    chat_type: str,
    selected_limit: Optional[int],
    speed_profile: str,
    concurrency_mode: str,
    state: FSMContext,
    bot: Bot,
    status_msg: Message
):
    type_label = TARGET_TYPE_LABELS.get(chat_type, "чат")
    await status_msg.edit_text("Инициализация целевой группы и запуск инвайт-задачи...")

    async with async_session_factory() as session:
        pending_total = (await session.execute(
            select(func.count(AudienceMember.id)).where(AudienceMember.status == "pending")
        )).scalar_one()

        task = InviteTask(
            target_group_id=target_group_id,
            speed_profile=speed_profile,
            concurrency_mode=concurrency_mode,
            max_invites=selected_limit,
            status="running",
            total_targets=pending_total
        )
        session.add(task)
        await session.commit()
        launched_task_id = task.id

    await state.clear()
    orchestrator = InviterOrchestrator(task_id=launched_task_id, concurrency_mode=concurrency_mode)

    chat_id = status_msg.chat.id
    message_id = status_msg.message_id

    async def update_status_ui(task_id: int, invited: int, total: int, floods: int, status_text: str, is_final: bool = False):
        is_immediate = is_final or ("добавлен" in status_text)
        if not invite_task_manager.should_update_ui(min_interval=1.5, is_final=is_immediate):
            return

        target_total = selected_limit if selected_limit else total
        percent = min(1.0, max(0.0, invited / target_total)) if target_total > 0 else 0.0
        bar_len = 10
        filled = int(round(bar_len * percent))
        bar = "█" * filled + "░" * (bar_len - filled)
        prog_bar = f"[{bar}] {int(percent * 100)}%"

        if "\n" in status_text:
            body = status_text
        else:
            body = f"• <b>Текущий статус:</b> <code>{quote_html(status_text)}</code>"

        ui_text = (
            "<b>TG-INVITE-MACHINE | Мониторинг кампании</b>\n"
            "────────────────────────\n"
            f"<b>Целевой чат:</b> <code>{quote_html(target_link)}</code>\n"
            f"<b>Прогресс:</b> <code>{prog_bar}</code> ({invited} / {target_total})\n"
            f"• <b>Флуд-паузы:</b> <code>{floods}</code>\n\n"
            f"{body}\n\n"
            "<blockquote>Прогресс инвайтинга обновляется в реальном времени.</blockquote>"
        )
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=ui_text,
                reply_markup=inviter_menu_keyboard(task_running=not is_final)
            )
        except Exception as exc:
            logger.debug("progress UI skipped: %s", exc)

    invite_task_manager.start(
        task_id=launched_task_id,
        orchestrator=orchestrator,
        coro=orchestrator.run(
            session_factory=async_session_factory,
            progress_callback=update_status_ui
        )
    )

    limit_desc = f"{selected_limit} участников" if selected_limit else "Без ограничений (вся база)"
    speed_desc = speed_profile.replace("custom:", "свой: ") if speed_profile.startswith("custom:") else speed_profile
    await status_msg.edit_text(
        "<b>TG-INVITE-MACHINE | Запуск кампании</b>\n"
        "────────────────────────\n"
        f"<b>Статус:</b> <code>[ RUNNING ] Задача #{launched_task_id} активна</code>\n\n"
        f"• <b>Целевой объект:</b> <code>{quote_html(target_link)}</code> ({type_label})\n"
        f"• <b>Установленный лимит:</b> <code>{limit_desc}</code>\n"
        f"• <b>Профиль задержки:</b> <code>{speed_desc}</code>\n\n"
        "<blockquote>Распределенный воркер начал обработку очереди.</blockquote>",
        reply_markup=inviter_menu_keyboard(task_running=True)
    )

@inviter_router.callback_query(F.data == "invite_pause")
async def callback_invite_pause(callback: CallbackQuery):
    orch = invite_task_manager.active_orchestrator
    if orch:
        orch.pause()
        await callback.answer("Инвайтинг поставлен на паузу.")
        await callback.message.edit_text(
            "Инвайтинг на паузе. Нажмите 'Возобновить' для продолжения.",
            reply_markup=inviter_menu_keyboard(task_running=True, is_paused=True)
        )
    else:
        await callback.answer("Нет активной задачи.", show_alert=True)

@inviter_router.callback_query(F.data == "invite_resume")
async def callback_invite_resume(callback: CallbackQuery):
    orch = invite_task_manager.active_orchestrator
    if orch:
        orch.resume()
        await callback.answer("Инвайтинг возобновлен.")
        await callback.message.edit_text(
            "Инвайтинг возобновлен и выполняется.",
            reply_markup=inviter_menu_keyboard(task_running=True, is_paused=False)
        )
    else:
        await callback.answer("Нет активной задачи.", show_alert=True)

@inviter_router.callback_query(F.data.startswith("invite_resume_paused_"))
async def callback_invite_resume_paused(callback: CallbackQuery, state: FSMContext, bot: Bot):
    raw_id = callback.data.replace("invite_resume_paused_", "")
    try:
        task_id = int(raw_id)
    except ValueError:
        await callback.answer("Неверный ID задачи.", show_alert=True)
        return

    if invite_task_manager.is_running():
        await callback.answer("Уже выполняется другая задача.", show_alert=True)
        return

    async with async_session_factory() as session:
        task = await session.get(InviteTask, task_id)
        if not task:
            await callback.answer("Задача не найдена.", show_alert=True)
            return
        target_group = await session.get(TargetGroup, task.target_group_id)
        if not target_group:
            await callback.answer("Целевая группа не найдена.", show_alert=True)
            return

        await auto_recover_cooldowns(session)
        await auto_assign_proxies(session)
        task.status = "running"
        await session.commit()
        target_link = target_group.username or str(target_group.tg_id)
        chat_type = target_group.chat_type or "supergroup"
        selected_limit = task.max_invites
        speed_profile = task.speed_profile

    await state.clear()
    status_msg = callback.message
    resumed_orchestrator = InviterOrchestrator(task_id=task_id)

    chat_id = status_msg.chat.id
    message_id = status_msg.message_id

    async def update_status_ui(t_id: int, invited: int, total: int, floods: int, status_text: str, is_final: bool = False):
        is_immediate = is_final or ("добавлен" in status_text)
        if not invite_task_manager.should_update_ui(min_interval=1.5, is_final=is_immediate):
            return

        target_total = selected_limit if selected_limit else total
        percent = min(1.0, max(0.0, invited / target_total)) if target_total > 0 else 0.0
        bar_len = 10
        filled = int(round(bar_len * percent))
        bar = "█" * filled + "░" * (bar_len - filled)
        prog_bar = f"[{bar}] {int(percent * 100)}%"

        if "\n" in status_text:
            body = status_text
        else:
            body = f"• <b>Текущий статус:</b> <code>{quote_html(status_text)}</code>"

        ui_text = (
            "<b>TG-INVITE-MACHINE | Мониторинг кампании</b>\n"
            "────────────────────────\n"
            f"<b>Целевой чат:</b> <code>{quote_html(target_link)}</code>\n"
            f"<b>Прогресс:</b> <code>{prog_bar}</code> ({invited} / {target_total})\n"
            f"• <b>Флуд-паузы:</b> <code>{floods}</code>\n\n"
            f"{body}\n\n"
            "<blockquote>Прогресс инвайтинга обновляется в реальном времени.</blockquote>"
        )
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=ui_text,
                reply_markup=inviter_menu_keyboard(task_running=not is_final)
            )
        except Exception as exc:
            logger.debug("progress UI skipped: %s", exc)

    invite_task_manager.start(
        task_id=task_id,
        orchestrator=resumed_orchestrator,
        coro=resumed_orchestrator.run(
            session_factory=async_session_factory,
            progress_callback=update_status_ui
        )
    )

    limit_desc = f"{selected_limit} участников" if selected_limit else "Без ограничений (вся база)"
    speed_desc = speed_profile.replace("custom:", "свой: ") if speed_profile.startswith("custom:") else speed_profile
    type_label = TARGET_TYPE_LABELS.get(chat_type, "чат")
    await status_msg.edit_text(
        "<b>TG-INVITE-MACHINE | Возобновление кампании</b>\n"
        "────────────────────────\n"
        f"<b>Статус:</b> <code>[ RUNNING ] Задача #{task_id} возобновлена</code>\n\n"
        f"• <b>Целевой объект:</b> <code>{quote_html(target_link)}</code> ({type_label})\n"
        f"• <b>Установленный лимит:</b> <code>{limit_desc}</code>\n"
        f"• <b>Профиль задержки:</b> <code>{speed_desc}</code>\n\n"
        "<blockquote>Распределенный воркер возобновил обработку очереди.</blockquote>",
        reply_markup=inviter_menu_keyboard(task_running=True)
    )
    await callback.answer("Задача возобновлена.")

@inviter_router.callback_query(F.data == "invite_stop")
async def callback_invite_stop(callback: CallbackQuery):
    orch = invite_task_manager.active_orchestrator
    stopped_id = invite_task_manager.active_task_id

    if orch:
        invite_task_manager.stop()
        if stopped_id is not None:
            try:
                async with async_session_factory() as session:
                    stopped_task = await session.get(InviteTask, stopped_id)
                    if stopped_task and stopped_task.status not in ("completed", "failed"):
                        stopped_task.status = "stopped"
                        stopped_task.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
                        await session.commit()
            except Exception as exc:
                logger.warning("stop bookkeeping failed for task %s: %s", stopped_id, exc)
        await callback.answer("Инвайтинг остановлен.")
        await callback.message.edit_text("Инвайтинг остановлен пользователем.", reply_markup=inviter_menu_keyboard(task_running=False))
    else:
        await callback.answer("Нет активной задачи.", show_alert=True)


@inviter_router.callback_query(F.data == "invite_history")
async def callback_invite_history(callback: CallbackQuery):
    async with async_session_factory() as session:
        members = (await session.execute(
            select(AudienceMember)
            .where(AudienceMember.status != "pending")
            .order_by(AudienceMember.updated_at.desc())
            .limit(15)
        )).scalars().all()

        total_processed = (await session.execute(
            select(func.count(AudienceMember.id))
            .where(AudienceMember.status != "pending")
        )).scalar_one()

        invited_count = (await session.execute(
            select(func.count(AudienceMember.id))
            .where(AudienceMember.status == "invited")
        )).scalar_one()

    if not members:
        await callback.answer("История инвайтов пока пуста. Запустите инвайтинг.", show_alert=True)
        return

    lines = []
    for m in members:
        user_label = f"@{m.username}" if m.username else (m.first_name or f"ID:{m.tg_id or m.id}")
        if m.status == "invited":
            tag = "[ УСПЕХ ] Добавлен"
        elif m.status == "restricted":
            tag = f"[ ПРОПУСК ] {m.reason or 'Приватность'}"
        elif m.status == "already_participant":
            tag = "[ ПРОПУСК ] Уже в группе"
        else:
            tag = f"[ {m.status.upper()} ] {m.reason or 'Ошибка'}"
        lines.append(f"• <code>{user_label}</code> - {tag}")

    history_content = "\n".join(lines)
    text = (
        "<b>TG-INVITE-MACHINE | История обработки аудитории</b>\n"
        "────────────────────────\n"
        f"• Всего обработано: <code>{total_processed}</code> чел.\n"
        f"• Успешно добавлено: <code>{invited_count}</code> чел.\n\n"
        f"<b>Последние попытки (до 15):</b>\n{history_content}"
    )
    await callback.message.edit_text(text, reply_markup=back_keyboard("nav_inviter"))
    await callback.answer()

