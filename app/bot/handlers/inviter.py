import asyncio
import time
from datetime import datetime, timezone
from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select, func

from app.core.config import settings
from app.core.database import async_session_factory
from app.models.models import Account, AudienceMember, TargetGroup, InviteTask
from app.bot.states import InviterState
from app.bot.keyboards import inviter_menu_keyboard, speed_profile_keyboard, back_keyboard
from app.services.inviter_service import InviterOrchestrator
from app.core.utils import normalize_chat_identifier, safe_edit_text

inviter_router = Router()

VALID_SPEED_PROFILES = {"cautious", "normal", "fast"}

active_orchestrator: InviterOrchestrator | None = None
active_task_handle: asyncio.Task | None = None
active_task_id: int | None = None
last_ui_update_time: float = 0.0

@inviter_router.callback_query(F.data == "nav_inviter")
async def callback_nav_inviter(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    is_running = active_orchestrator is not None and active_task_handle is not None and not active_task_handle.done()

    async with async_session_factory() as session:
        pending_count = (await session.execute(
            select(func.count(AudienceMember.id)).where(AudienceMember.status == "pending")
        )).scalar_one()
        active_accounts = (await session.execute(
            select(func.count(Account.id)).where(Account.is_active == True, Account.status == "active")
        )).scalar_one()

    status_line = "Запущен" if is_running else "Остановлен"
    text = (
        "Управление инвайтером.\n\n"
        f"Текущее состояние: {status_line}\n"
        f"Профиль скорости: {settings.DEFAULT_SPEED_PROFILE}\n"
        f"Пользователей в очереди: {pending_count}\n"
        f"Готовых аккаунтов: {active_accounts}\n"
    )
    await safe_edit_text(callback.message, text, reply_markup=inviter_menu_keyboard(task_running=is_running))
    await callback.answer()

@inviter_router.callback_query(F.data == "invite_speed")
async def callback_invite_speed(callback: CallbackQuery):
    text = (
        "Выберите профиль скорости инвайта:\n\n"
        "Осторожный (50-110 сек): минимальный риск, для свежих аккаунтов.\n"
        "Обычный (35-75 сек): рекомендуемый баланс скорости и надежности.\n"
        "Быстрый (17-37 сек): повышенная скорость, для прогретых аккаунтов."
    )
    await safe_edit_text(callback.message, text, reply_markup=speed_profile_keyboard())
    await callback.answer()

@inviter_router.callback_query(F.data.startswith("set_speed_"))
async def callback_set_speed(callback: CallbackQuery, state: FSMContext):
    profile = callback.data.replace("set_speed_", "")
    if profile not in VALID_SPEED_PROFILES:
        await callback.answer("Недопустимый профиль скорости.", show_alert=True)
        return
    settings.DEFAULT_SPEED_PROFILE = profile
    await callback.answer(f"Установлен профиль: {profile}")
    await callback_nav_inviter(callback, state)

@inviter_router.callback_query(F.data == "invite_start")
async def callback_invite_start(callback: CallbackQuery, state: FSMContext):
    global active_orchestrator, active_task_handle

    if active_orchestrator and active_task_handle and not active_task_handle.done():
        if active_orchestrator.is_paused:
            active_orchestrator.resume()
            await callback.answer("Инвайтинг возобновлен.")
            await callback.message.edit_text("Инвайтинг возобновлен.", reply_markup=inviter_menu_keyboard(task_running=True, is_paused=False))
            return
        await callback.answer("Инвайтер уже запущен.", show_alert=True)
        return

    async with async_session_factory() as session:
        accounts_count = (await session.execute(
            select(func.count(Account.id)).where(Account.is_active == True, Account.status == "active")
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
        "Введите ссылку или @username целевой группы, куда приглашать людей:\n"
        "Например: @my_target_community или https://t.me/my_target_community\n\n"
        "Убедитесь, что аккаунты из пула состоят в группе или имеют права на добавление."
    )
    await callback.message.edit_text(text, reply_markup=back_keyboard("nav_inviter"))
    await callback.answer()

@inviter_router.message(InviterState.waiting_for_target_group, F.text)
async def handle_target_group(message: Message, state: FSMContext, bot: Bot):
    global active_orchestrator, active_task_handle, active_task_id, last_ui_update_time

    target_link = message.text.strip()
    clean_username = normalize_chat_identifier(target_link)
    status_msg = await message.answer("Инициализация целевой группы и запуск инвайт-задачи...")

    async with async_session_factory() as session:
        target_group = (await session.execute(
            select(TargetGroup).where(TargetGroup.username == clean_username)
        )).scalars().first()

        if not target_group:
            target_group = TargetGroup(
                title=clean_username,
                username=clean_username,
                is_active=True
            )
            session.add(target_group)
            await session.commit()

        pending_total = (await session.execute(
            select(func.count(AudienceMember.id)).where(AudienceMember.status == "pending")
        )).scalar_one()

        task = InviteTask(
            target_group_id=target_group.id,
            speed_profile=settings.DEFAULT_SPEED_PROFILE,
            status="running",
            total_targets=pending_total
        )
        session.add(task)
        await session.commit()
        active_task_id = task.id

    await state.clear()
    active_orchestrator = InviterOrchestrator(task_id=active_task_id)

    chat_id = status_msg.chat.id
    message_id = status_msg.message_id

    async def update_status_ui(task_id: int, invited: int, total: int, floods: int, status_text: str):
        global last_ui_update_time
        current_now = time.monotonic()
        if current_now - last_ui_update_time < 3.0:
            return
        last_ui_update_time = current_now

        ui_text = (
            f"Инвайтинг в {target_link}:\n\n"
            f"Приглашено: {invited} из {total}\n"
            f"Флуд-пауз: {floods}\n"
            f"Статус: {status_text}"
        )
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=ui_text,
                reply_markup=inviter_menu_keyboard(task_running=True)
            )
        except Exception:
            pass

    active_task_handle = asyncio.create_task(
        active_orchestrator.run(
            session_factory=async_session_factory,
            progress_callback=update_status_ui
        )
    )

    await status_msg.edit_text(
        f"Задача #{active_task_id} запущена.\nЦелевая группа: {target_link}\nПрофиль: {settings.DEFAULT_SPEED_PROFILE}",
        reply_markup=inviter_menu_keyboard(task_running=True)
    )

@inviter_router.callback_query(F.data == "invite_pause")
async def callback_invite_pause(callback: CallbackQuery):
    global active_orchestrator
    if active_orchestrator:
        active_orchestrator.pause()
        await callback.answer("Инвайтинг поставлен на паузу.")
        await callback.message.edit_text(
            "Инвайтинг на паузе. Нажмите 'Возобновить' для продолжения.",
            reply_markup=inviter_menu_keyboard(task_running=True, is_paused=True)
        )
    else:
        await callback.answer("Нет активной задачи.", show_alert=True)

@inviter_router.callback_query(F.data == "invite_resume")
async def callback_invite_resume(callback: CallbackQuery):
    global active_orchestrator
    if active_orchestrator:
        active_orchestrator.resume()
        await callback.answer("Инвайтинг возобновлен.")
        await callback.message.edit_text(
            "Инвайтинг возобновлен и выполняется.",
            reply_markup=inviter_menu_keyboard(task_running=True, is_paused=False)
        )
    else:
        await callback.answer("Нет активной задачи.", show_alert=True)

@inviter_router.callback_query(F.data == "invite_stop")
async def callback_invite_stop(callback: CallbackQuery):
    global active_orchestrator, active_task_handle, active_task_id
    if active_orchestrator:
        active_orchestrator.stop()
        if active_task_handle and not active_task_handle.done():
            active_task_handle.cancel()
        stopped_id = active_task_id
        active_orchestrator = None
        active_task_handle = None
        active_task_id = None
        if stopped_id is not None:
            try:
                async with async_session_factory() as session:
                    stopped_task = await session.get(InviteTask, stopped_id)
                    if stopped_task and stopped_task.status not in ("completed", "failed"):
                        stopped_task.status = "stopped"
                        stopped_task.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
                        await session.commit()
            except Exception:
                pass
        await callback.answer("Инвайтинг остановлен.")
        await callback.message.edit_text("Инвайтинг остановлен пользователем.", reply_markup=inviter_menu_keyboard(task_running=False))
    else:
        await callback.answer("Нет активной задачи.", show_alert=True)
