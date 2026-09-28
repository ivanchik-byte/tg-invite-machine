import asyncio
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from html import escape as quote_html
from typing import Optional

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, FSInputFile
from sqlalchemy import select, func, delete
from sqlalchemy.orm import selectinload

from app.core.config import DATA_DIR
from app.core.database import async_session_factory
from app.models.models import Account, AudienceMember, AudienceHistory
from app.bot.states import ParserState
from app.bot.keyboards import parser_menu_keyboard, back_keyboard, main_menu_keyboard
from app.bot.handlers.menu import build_main_dashboard_text
from app.services.collector_service import collect_chat_members
from app.core.utils import normalize_chat_identifier, safe_edit_text

parser_router = Router()

@parser_router.message(Command("cancel"))
async def handle_parser_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.")
    dashboard_text = await build_main_dashboard_text()
    await message.answer(dashboard_text, reply_markup=main_menu_keyboard())

@parser_router.callback_query(F.data == "nav_parser")
async def callback_nav_parser(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with async_session_factory() as session:
        pending_count = (await session.execute(
            select(func.count(AudienceMember.id)).where(AudienceMember.status == "pending")
        )).scalar_one()
        invited_count = (await session.execute(
            select(func.count(AudienceMember.id)).where(AudienceMember.status == "invited")
        )).scalar_one()
        restricted_count = (await session.execute(
            select(func.count(AudienceMember.id)).where(AudienceMember.status == "restricted")
        )).scalar_one()
        history_count = (await session.execute(
            select(func.count(AudienceHistory.id))
        )).scalar_one()

    text = (
        "<b>TG-INVITE-MACHINE | Модуль сбора аудитории</b>\n"
        "────────────────────────\n"
        f"<b>Текущая очередь контактов:</b> <code>{pending_count + invited_count + restricted_count}</code> чел.\n"
        f"• <b>Ожидают инвайта:</b> <code>{pending_count}</code> чел.\n"
        f"• <b>Успешно добавлено:</b> <code>{invited_count}</code> чел.\n"
        f"• <b>Приватные профили:</b> <code>{restricted_count}</code> чел.\n"
        f"• <b>Реестр дедупликации (БД истории):</b> <code>{history_count}</code> аккаунтов\n\n"
        "<i>Дедупликация активна: парсер автоматически пропускает профили, уже сохраненные в базе.</i>\n\n"
        "<b>Доступные режимы сбора:</b>\n"
        "• <b>Все участники:</b> перебор по алфавиту (обход лимита Telegram в 10,000)\n"
        "• <b>Активные авторы:</b> парсинг пользователей по истории сообщений за 1-30 дней\n\n"
        "<blockquote>Сбор возможен только из открытых групп или супергрупп, где список участников не скрыт администрацией.</blockquote>"
    )
    await safe_edit_text(callback.message, text, reply_markup=parser_menu_keyboard())
    await callback.answer()

@parser_router.callback_query(F.data == "parse_all")
async def callback_parse_all(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ParserState.waiting_for_chat)
    await state.update_data(mode="all")
    text = (
        "<b>Сбор всех участников супергруппы</b>\n"
        "────────────────────────\n"
        "Введите ссылку или @username открытого чата/канала:\n\n"
        "<i>Пример:</i> <code>@python_community</code> или <code>https://t.me/example_group</code>\n\n"
        "<i>Для отмены отправьте /cancel или нажмите «Назад».</i>"
    )
    await safe_edit_text(callback.message, text, reply_markup=back_keyboard("nav_parser"))
    await callback.answer()

@parser_router.callback_query(F.data == "parse_active")
async def callback_parse_active(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ParserState.waiting_for_chat)
    await state.update_data(mode="active")
    text = (
        "<b>Сбор активных авторов сообщений</b>\n"
        "────────────────────────\n"
        "Введите ссылку или @username чата для анализа активности:\n\n"
        "<i>Пример:</i> <code>@tech_community</code> или <code>https://t.me/tech_community</code>\n\n"
        "<i>Для отмены отправьте /cancel или нажмите «Назад».</i>"
    )
    await safe_edit_text(callback.message, text, reply_markup=back_keyboard("nav_parser"))
    await callback.answer()

@parser_router.message(ParserState.waiting_for_chat, F.text)
async def handle_parser_chat(message: Message, state: FSMContext):
    chat_link = message.text.strip()
    if chat_link.startswith("/cancel"):
        await handle_parser_cancel(message, state)
        return
    state_data = await state.get_data()
    mode = state_data.get("mode", "all")

    if mode == "active":
        await state.update_data(chat_link=chat_link)
        await state.set_state(ParserState.waiting_for_days)
        await message.answer("За сколько последних дней собирать авторов сообщений? (Введите число, например 3 или 7):")
        return

    await execute_parsing(message, state, chat_link, active_days=None)

@parser_router.message(ParserState.waiting_for_days, F.text)
async def handle_parser_days(message: Message, state: FSMContext):
    if message.text.strip().startswith("/cancel"):
        await handle_parser_cancel(message, state)
        return
    try:
        days = int(message.text.strip())
        if days <= 0 or days > 30:
            raise ValueError
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число дней от 1 до 30 (или /cancel для отмены):")
        return

    state_data = await state.get_data()
    chat_link = state_data["chat_link"]
    await execute_parsing(message, state, chat_link, active_days=days)

async def execute_parsing(message: Message, state: FSMContext, chat_identifier: str, active_days: int | None):
    clean_identifier = normalize_chat_identifier(chat_identifier)
    status_msg = await message.answer("Поиск доступного рабочего аккаунта для парсинга...")

    async with async_session_factory() as session:
        account = (await session.execute(
            select(Account)
            .options(selectinload(Account.proxy))
            .where(Account.is_active == True, Account.status == "active")
            .limit(1)
        )).scalars().first()

    if not account:
        await safe_edit_text(
            status_msg,
            "В пуле нет ни одного активного аккаунта. Сначала добавьте аккаунт в разделе 'Аккаунты'."
        )
        await state.clear()
        dashboard_text = await build_main_dashboard_text()
        await message.answer(dashboard_text, reply_markup=main_menu_keyboard())
        return

    recent_users: deque[str] = deque(maxlen=5)
    last_ui_update = 0.0

    async def report_progress(count: int, stage_text: str, new_user_tag: Optional[str] = None):
        nonlocal last_ui_update
        if new_user_tag:
            recent_users.appendleft(new_user_tag)

        now = asyncio.get_event_loop().time()
        if now - last_ui_update < 1.3 and count > 0:
            return
        last_ui_update = now

        items_str = "\n".join([f"{idx}. {u}" for idx, u in enumerate(recent_users, 1)])
        if not items_str:
            items_str = "<i>Поиск участников...</i>"

        text = (
            "<b>TG-INVITE-MACHINE | Сбор аудитории</b>\n"
            "────────────────────────\n"
            f"• Источник: <code>{quote_html(clean_identifier)}</code>\n"
            f"• Статус: <code>{stage_text}</code>\n"
            f"• Найдено участников: <code>{count}</code> чел.\n\n"
            "<b>Последние найденные аккаунты (макс. 5):</b>\n"
            f"{items_str}\n\n"
            "<i>Новые аккаунты появляются сверху, старые смещаются вниз.</i>"
        )
        await safe_edit_text(status_msg, text)

    try:
        async with async_session_factory() as session:
            new_added, skipped, export_path = await collect_chat_members(
                session=session,
                account=account,
                chat_identifier=clean_identifier,
                active_days=active_days,
                progress_callback=report_progress
            )

        final_users_str = "\n".join([f"{idx}. {u}" for idx, u in enumerate(recent_users, 1)])
        if not final_users_str:
            final_users_str = "<i>(нет новых записей)</i>"

        result_text = (
            "<b>TG-INVITE-MACHINE | Сбор аудитории завершен</b>\n"
            "────────────────────────\n"
            f"• Источник: <code>{quote_html(clean_identifier)}</code>\n"
            f"• Добавлено новых профилей: <code>{new_added}</code>\n"
            f"• Пропущено (дубликаты/уже в базе): <code>{skipped}</code>\n\n"
            "<b>Последние собранные профили:</b>\n"
            f"{final_users_str}\n\n"
            "Файл с результатами выгрузки сформирован."
        )
        await safe_edit_text(status_msg, result_text)

        if export_path and export_path.exists():
            document_file = FSInputFile(export_path, filename=export_path.name)
            await message.answer_document(
                document_file,
                caption=f"<b>[ВЫГРУЗКА] Результаты сбора аудитории</b>\n• Источник: <code>{quote_html(chat_identifier)}</code>\n• Добавлено в базу: <code>{new_added}</code> чел."
            )

        dashboard_text = await build_main_dashboard_text()
        await message.answer(dashboard_text, reply_markup=main_menu_keyboard())

    except Exception as exc:
        await safe_edit_text(
            status_msg,
            f"<b>[ОШИБКА] Сбой при сборе аудитории</b>\n\nПричина: <code>{quote_html(str(exc))}</code>"
        )
        dashboard_text = await build_main_dashboard_text()
        await message.answer(dashboard_text, reply_markup=main_menu_keyboard())
    finally:
        await state.clear()

@parser_router.callback_query(F.data == "parse_export")
async def callback_parse_export(callback: CallbackQuery):
    async with async_session_factory() as session:
        total = (await session.execute(
            select(func.count(AudienceMember.id))
        )).scalar_one()

    if not total:
        await safe_edit_text(
            callback.message,
            "<b>[ВНИМАНИЕ] База аудитории пуста</b>\n\nВ базе еще нет собранных пользователей. Сначала запустите сбор аудитории из чата.",
            reply_markup=back_keyboard("nav_parser")
        )
        await callback.answer()
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    export_path = DATA_DIR / "exports" / f"audience_{timestamp}.txt"
    export_path.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    # stream export in chunks to keep memory footprint bounded
    with open(export_path, "w", encoding="utf-8") as out:
        offset = 0
        while True:
            async with async_session_factory() as session:
                page = (await session.execute(
                    select(AudienceMember.username, AudienceMember.tg_id)
                    .order_by(AudienceMember.id.asc())
                    .offset(offset).limit(1000)
                )).all()
            if not page:
                break
            for username, tg_id in page:
                out.write(f"@{username}\n" if username else f"{tg_id}\n")
                written += 1
            offset += len(page)

    document_file = FSInputFile(export_path, filename=f"audience_{timestamp}.txt")
    await callback.message.answer_document(
        document_file,
        caption=(
            f"<b>TG-INVITE-MACHINE | Выгрузка базы аудитории</b>\n"
            f"Всего пользователей: <code>{written}</code> чел.\n"
            f"Формат: @username или ID (по одной записи на строку)"
        )
    )
    await callback.answer()

@parser_router.callback_query(F.data == "parse_clear")
async def callback_parse_clear(callback: CallbackQuery, state: FSMContext):
    async with async_session_factory() as session:
        deleted = (await session.execute(delete(AudienceMember))).rowcount
        await session.commit()

    if deleted == 0:
        await callback.answer("База аудитории уже пуста.", show_alert=True)
    else:
        await callback.answer(f"Очередь инвайта очищена ({deleted} шт.). База дедупликации сохранена.", show_alert=True)
    await callback_nav_parser(callback, state)
