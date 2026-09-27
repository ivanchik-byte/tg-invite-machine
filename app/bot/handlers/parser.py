import asyncio
from datetime import datetime, timezone
from pathlib import Path
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, FSInputFile
from html import escape as quote_html
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.core.config import DATA_DIR
from app.core.database import async_session_factory
from app.models.models import Account, AudienceMember
from app.bot.states import ParserState
from app.bot.keyboards import parser_menu_keyboard, back_keyboard
from app.services.collector_service import collect_chat_members
from app.core.utils import normalize_chat_identifier, safe_edit_text

parser_router = Router()

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

    text = (
        "<b>TG-INVITE-MACHINE | Модуль сбора аудитории</b>\n"
        "────────────────────────\n"
        f"<b>Текущая база контактов:</b> <code>{pending_count + invited_count + restricted_count}</code> чел.\n"
        f"• <b>Ожидают инвайта:</b> <code>{pending_count}</code> чел.\n"
        f"• <b>Успешно добавлено:</b> <code>{invited_count}</code> чел.\n"
        f"• <b>Приватные профили:</b> <code>{restricted_count}</code> чел.\n\n"
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
    try:
        days = int(message.text.strip())
        if days <= 0 or days > 30:
            raise ValueError
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число дней от 1 до 30:")
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
            "В пуле нет ни одного активного аккаунта. Сначала добавьте аккаунт в разделе 'Аккаунты'.",
            reply_markup=back_keyboard("nav_parser")
        )
        await state.clear()
        return

    async def report_progress(count: int, stage_text: str):
        await safe_edit_text(status_msg, f"Статус сбора: {stage_text}")

    try:
        async with async_session_factory() as session:
            new_added, skipped, export_path = await collect_chat_members(
                session=session,
                account=account,
                chat_identifier=clean_identifier,
                active_days=active_days,
                progress_callback=report_progress
            )

        result_text = (
            f"Сбор успешно завершен.\n"
            f"Добавлено новых профилей в очередь: {new_added}\n"
            f"Пропущено дублей/уже собранных: {skipped}\n\n"
            "Файл с выгрузкой прикреплен ниже."
        )
        await status_msg.edit_text(result_text)

        if export_path and export_path.exists():
            document_file = FSInputFile(export_path, filename=export_path.name)
            await message.answer_document(document_file, caption=f"Выгрузка из {quote_html(chat_identifier)}")

    except Exception as exc:
        await status_msg.edit_text(f"Ошибка при сборе аудитории: {quote_html(str(exc))}", reply_markup=back_keyboard("nav_parser"))
    finally:
        await state.clear()

@parser_router.callback_query(F.data == "parse_export")
async def callback_parse_export(callback: CallbackQuery):
    async with async_session_factory() as session:
        members = (await session.execute(
            select(AudienceMember)
        )).scalars().all()

    if not members:
        await safe_edit_text(callback.message, "В базе еще нет собранных пользователей. Сначала запустите сбор аудитории из чата.", reply_markup=back_keyboard("nav_parser"))
        await callback.answer()
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    export_path = DATA_DIR / "exports" / f"audience_{timestamp}.txt"
    export_path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    for m in members:
        if m.username:
            lines.append(f"@{m.username}")
        elif m.tg_id:
            lines.append(str(m.tg_id))

    await asyncio.to_thread(export_path.write_text, "\n".join(lines), encoding="utf-8")
    document_file = FSInputFile(export_path, filename=f"audience_{timestamp}.txt")
    await callback.message.answer_document(
        document_file,
        caption=(
            f"<b>TG-INVITE-MACHINE | Выгрузка базы аудитории</b>\n"
            f"Всего пользователей: <code>{len(lines)}</code> чел.\n"
            f"Формат: @username или ID (по одной записи на строку)"
        )
    )
    await callback.answer()
