import shutil
import tempfile
from pathlib import Path
from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, Document
from html import escape as quote_html
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.core.config import settings, TEMP_DIR
from app.core.database import async_session_factory
from app.core.security import safe_extract_zip, encrypt_session_string
from app.models.models import Account, Proxy
from app.bot.states import AccountState
from app.bot.keyboards import accounts_menu_keyboard, back_keyboard, accounts_pagination_keyboard
from app.core.utils import safe_edit_text
from app.telegram.converter import convert_tdata_archive, import_session_file, PASSWORD_REQUIRED
from app.telegram.client_factory import get_telethon_client, decrypt_proxy_password, build_proxy_dict, ProxySecurityError
from app.services.account_service import import_account_bundle, register_single_account
from app.services.spambot_service import check_account_spambot
from app.services.proxy_service import import_proxies_from_text
from app.services.export_service import generate_accounts_excel

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB hard cap
MAX_SESSION_FILES = 20

accounts_router = Router()

@accounts_router.callback_query(F.data == "nav_accounts")
async def callback_nav_accounts(callback: CallbackQuery, state: FSMContext | None = None):
    if state is not None:
        await state.clear()
    async with async_session_factory() as session:
        count = (await session.execute(select(func.count(Account.id)))).scalar_one()

    text = (
        f"Управление аккаунтами Telegram.\n"
        f"Текущее количество в базе: {count} шт.\n\n"
        "Отправьте архив TData в формате .zip или одиночный файл .session прямо в чат."
    )
    await safe_edit_text(callback.message, text, reply_markup=accounts_menu_keyboard(has_accounts=count > 0))
    await callback.answer()

@accounts_router.callback_query(F.data == "acc_upload_tdata")
async def callback_upload_tdata(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AccountState.waiting_for_file)
    await state.update_data(expected_type="tdata")
    text = (
        "Отправьте ZIP архив с папкой tdata документом в этот чат.\n"
        "Если на аккаунте установлен пароль двухэтапной аутентификации (2FA), бот запросит его следующим шагом."
    )
    await callback.message.edit_text(text, reply_markup=back_keyboard("nav_accounts"))
    await callback.answer()

@accounts_router.callback_query(F.data == "acc_upload_session")
async def callback_upload_session(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AccountState.waiting_for_file)
    await state.update_data(expected_type="session")
    text = "Отправьте файл с расширением .session документом в этот чат."
    await callback.message.edit_text(text, reply_markup=back_keyboard("nav_accounts"))
    await callback.answer()

@accounts_router.message(AccountState.waiting_for_file, F.document)
async def handle_account_file(message: Message, state: FSMContext, bot: Bot):
    document: Document = message.document
    filename = Path(document.file_name or "upload").name
    state_data = await state.get_data()
    expected_type = state_data.get("expected_type")

    if document.file_size and document.file_size > MAX_UPLOAD_BYTES:
        await state.clear()
        await message.answer(
            f"Файл слишком большой (максимум {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ).",
            reply_markup=back_keyboard("nav_accounts")
        )
        return

    status_msg = await message.answer("Загрузка и обработка файла...")
    temp_target = TEMP_DIR / f"{document.file_id}_{filename}"

    try:
        await bot.download(document, destination=temp_target)

        if filename.lower().endswith(".zip"):
            try:
                bundle_result = await import_account_bundle(
                    temp_target,
                    session_factory=async_session_factory,
                    max_sessions=MAX_SESSION_FILES
                )
            except ProxySecurityError:
                await state.clear()
                await status_msg.edit_text(
                    "Включена политика Zero-Leak, но в базе нет активных прокси. Добавьте прокси перед импортом архива.",
                    reply_markup=back_keyboard("nav_accounts")
                )
                return

            if bundle_result is not None:
                await state.clear()
                await status_msg.edit_text(
                    f"Пакетный импорт архива завершен.\n"
                    f"Успешно добавлено сессий: {bundle_result.imported_count}\n"
                    f"Ошибок: {bundle_result.errors_count}\n"
                    f"Добавлено прокси: {bundle_result.added_proxies} новых",
                    reply_markup=back_keyboard("nav_accounts")
                )
                return

        async with async_session_factory() as session:
            single_proxy = (await session.execute(
                select(Proxy).where(Proxy.is_active == True).order_by(Proxy.id.asc()).limit(1)
            )).scalars().first()

        if settings.REQUIRE_STRICT_PROXIES and not single_proxy:
            await state.clear()
            await status_msg.edit_text(
                "Включена политика Zero-Leak, но в базе нет активных прокси. Добавьте рабочий прокси перед загрузкой аккаунтов.",
                reply_markup=back_keyboard("nav_accounts")
            )
            return

        single_proxy_dict = build_proxy_dict(single_proxy) if single_proxy else None
        chosen_proxy_id = single_proxy.id if single_proxy else None

        if filename.lower().endswith(".zip"):
            success, msg, info = await convert_tdata_archive(temp_target, proxy=single_proxy_dict)
            if not success and msg == PASSWORD_REQUIRED:
                await state.set_state(AccountState.waiting_for_password)
                await state.update_data(archive_path=str(temp_target))
                await status_msg.edit_text("Для этой TData требуется пароль двухэтапной аутентификации. Введите его в ответном сообщении и удалите сообщение после отправки:")
                return

        elif expected_type == "session" or filename.lower().endswith(".session"):
            success, msg, info = await import_session_file(temp_target, proxy=single_proxy_dict)
        else:
            await state.clear()
            await status_msg.edit_text("Неподдерживаемый формат файла. Отправьте .zip архив с tdata или .session файл.")
            return

        if not success or not info:
            await state.clear()
            await status_msg.edit_text(f"Не удалось добавить аккаунт: {quote_html(msg or '')}")
            return

        async with async_session_factory() as session:
            acc, is_new = await register_single_account(session, info, proxy_id=chosen_proxy_id)
            if is_new:
                name_display = quote_html(info.get('first_name') or info['phone'])
                username_display = f" (@{quote_html(info['username'])})" if info.get('username') else ""
                response_text = f"Аккаунт {name_display}{username_display} успешно добавлен в пул."
            else:
                response_text = f"Аккаунт {quote_html(info['phone'])} обновлен в базе."

        await state.clear()
        await status_msg.edit_text(response_text, reply_markup=back_keyboard("nav_accounts"))

    finally:
        current_state = await state.get_state()
        if temp_target.exists() and current_state != AccountState.waiting_for_password.state:
            temp_target.unlink(missing_ok=True)

@accounts_router.message(AccountState.waiting_for_password, F.text)
async def handle_account_password(message: Message, state: FSMContext):
    state_data = await state.get_data()
    archive_path = state_data.get("archive_path")
    if not archive_path or not Path(archive_path).exists():
        await state.clear()
        await message.answer("Файл архива не найден, повторите загрузку заново.", reply_markup=back_keyboard("nav_accounts"))
        return

    password = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass
    status_msg = await message.answer("Проверка пароля и конвертация сессии...")

    async with async_session_factory() as session:
        active_proxy = (await session.execute(
            select(Proxy).where(Proxy.is_active == True).order_by(Proxy.id.asc()).limit(1)
        )).scalars().first()

    if settings.REQUIRE_STRICT_PROXIES and not active_proxy:
        await state.clear()
        await status_msg.edit_text(
            "Включена политика Zero-Leak, но в базе нет активных прокси. Добавьте рабочий прокси перед подтверждением пароля.",
            reply_markup=back_keyboard("nav_accounts")
        )
        return

    proxy_dict = build_proxy_dict(active_proxy) if active_proxy else None
    chosen_proxy_id = active_proxy.id if active_proxy else None

    success, msg, info = await convert_tdata_archive(Path(archive_path), password=password, proxy=proxy_dict)
    if not success or not info:
        attempts = state_data.get("attempts", 0) + 1
        if attempts >= 3:
            Path(archive_path).unlink(missing_ok=True)
            await state.clear()
            await status_msg.edit_text(
                f"Ошибка авторизации: {quote_html(msg or '')}\nПревышено количество попыток. Загрузите архив заново.",
                reply_markup=back_keyboard("nav_accounts")
            )
            return

        await state.update_data(attempts=attempts)
        await status_msg.edit_text(
            f"Ошибка авторизации: {quote_html(msg or '')}\nПопытка {attempts} из 3. Введите пароль еще раз:",
            reply_markup=back_keyboard("nav_accounts")
        )
        return

    try:
        async with async_session_factory() as session:
            acc, is_new = await register_single_account(session, info, proxy_id=chosen_proxy_id)
            if is_new:
                response_text = f"Аккаунт {quote_html(info['phone'])} успешно авторизован и сохранен."
            else:
                response_text = f"Аккаунт {quote_html(info['phone'])} успешно обновлен с 2FA паролем."

        await state.clear()
        await status_msg.edit_text(response_text, reply_markup=back_keyboard("nav_accounts"))

    finally:
        Path(archive_path).unlink(missing_ok=True)


@accounts_router.callback_query(F.data.startswith("acc_list_"))
async def callback_list_accounts(callback: CallbackQuery):
    try:
        offset = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        offset = 0

    limit = 8

    async with async_session_factory() as session:
        accounts = (await session.execute(
            select(Account).order_by(Account.id.asc()).offset(offset).limit(limit)
        )).scalars().all()
        total = (await session.execute(select(func.count(Account.id)))).scalar_one()

    if not accounts:
        await safe_edit_text(callback.message, "Аккаунты не найдены.", reply_markup=back_keyboard("nav_accounts"))
        await callback.answer()
        return

    page_num = (offset // limit) + 1
    total_pages = max(1, (total + limit - 1) // limit)
    lines = [f"Список аккаунтов (всего: {total}, стр. {page_num}/{total_pages}):\n"]
    for acc in accounts:
        display_name = acc.username and f"@{acc.username}" or acc.first_name or acc.phone
        lines.append(f"#{acc.id} {quote_html(display_name)} [{acc.status}] (инвайтов сегодня: {acc.daily_invites_count})")

    await safe_edit_text(
        callback.message,
        "\n".join(lines),
        reply_markup=accounts_pagination_keyboard(offset=offset, limit=limit, total=total)
    )
    await callback.answer()

@accounts_router.callback_query(F.data == "acc_check_all")
async def callback_check_all(callback: CallbackQuery):
    status_msg = await callback.message.edit_text("Запуск проверки подключения всех аккаунтов...")

    async with async_session_factory() as session:
        accounts = (await session.execute(
            select(Account).options(selectinload(Account.proxy))
        )).scalars().all()

    valid_count = 0
    banned_count = 0

    for idx, acc in enumerate(accounts, 1):
        client = get_telethon_client(acc)
        try:
            await client.connect()
            if await client.is_user_authorized():
                user = await client.get_me()
                async with async_session_factory() as session:
                    db_acc = await session.get(Account, acc.id)
                    if db_acc:
                        db_acc.status = "active"
                        db_acc.first_name = user.first_name
                        db_acc.username = user.username
                        await session.commit()
                valid_count += 1
            else:
                async with async_session_factory() as session:
                    db_acc = await session.get(Account, acc.id)
                    if db_acc:
                        db_acc.status = "banned"
                        await session.commit()
                banned_count += 1
        except Exception:
            banned_count += 1
        finally:
            await client.disconnect()

        if idx % 3 == 0 or idx == len(accounts):
            await safe_edit_text(status_msg, f"Проверено {idx}/{len(accounts)} аккаунтов...\nВалидных: {valid_count}, Недоступных: {banned_count}")

    await safe_edit_text(
        status_msg,
        f"Проверка завершена.\nВалидных аккаунтов: {valid_count}\nОтозванных/недоступных: {banned_count}",
        reply_markup=back_keyboard("nav_accounts")
    )
    await callback.answer()

@accounts_router.callback_query(F.data == "acc_check_spambot")
async def callback_check_spambot(callback: CallbackQuery):
    status_msg = await callback.message.edit_text("Запуск проверки аккаунтов через @SpamBot...")

    async with async_session_factory() as session:
        accounts = (await session.execute(
            select(Account).options(selectinload(Account.proxy)).where(Account.status != "banned")
        )).scalars().all()

    clean_count = 0
    limited_count = 0

    for idx, acc in enumerate(accounts, 1):
        status, reason = await check_account_spambot(acc)
        async with async_session_factory() as session:
            db_acc = await session.get(Account, acc.id)
            if db_acc:
                db_acc.status = status
                await session.commit()

        if status == "active":
            clean_count += 1
        elif status == "spambot":
            limited_count += 1

        if idx % 2 == 0 or idx == len(accounts):
            await safe_edit_text(status_msg, f"Проверено через @SpamBot {idx}/{len(accounts)}...\nЧистых: {clean_count}, Со спамблоком: {limited_count}")

    await safe_edit_text(
        status_msg,
        f"Проверка @SpamBot завершена.\nБез ограничений: {clean_count}\nСо спамблоком: {limited_count}",
        reply_markup=back_keyboard("nav_accounts")
    )
    await callback.answer()

@accounts_router.callback_query(F.data == "acc_export_excel")
async def callback_export_excel(callback: CallbackQuery):
    async with async_session_factory() as session:
        accounts = (await session.execute(
            select(Account).options(selectinload(Account.proxy)).order_by(Account.id.asc())
        )).scalars().all()

    if not accounts:
        await callback.answer("В базе нет аккаунтов для выгрузки.", show_alert=True)
        return

    excel_file = generate_accounts_excel(accounts)
    await callback.message.answer_document(
        document=excel_file,
        caption=f"Аудиторская выгрузка: {len(accounts)} аккаунтов в формате Excel."
    )
    await callback.answer()

@accounts_router.callback_query(F.data == "acc_purge_banned")
async def callback_purge_banned(callback: CallbackQuery, state: FSMContext):
    async with async_session_factory() as session:
        banned = (await session.execute(
            select(Account).where(
                (Account.status.in_(["banned", "deactivated", "spambot"])) |
                (Account.is_active == False)
            )
        )).scalars().all()

        if not banned:
            await callback.answer("В базе нет забаненных или неактивных аккаунтов.", show_alert=True)
            return

        banned_count = len(banned)
        for acc in banned:
            await session.delete(acc)
        await session.commit()

    await callback.answer(f"Удалено {banned_count} неактивных/забаненных аккаунтов.", show_alert=True)
    await callback_nav_accounts(callback, state)
