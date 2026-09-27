import shutil
import tempfile
from pathlib import Path
from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, Document
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.core.config import settings, TEMP_DIR
from app.core.database import async_session_factory
from app.core.security import safe_extract_zip
from app.models.models import Account, Proxy
from app.bot.states import AccountState
from app.bot.keyboards import accounts_menu_keyboard, back_keyboard, accounts_pagination_keyboard
from app.core.utils import safe_edit_text
from app.telegram.converter import convert_tdata_archive, import_session_file
from app.telegram.client_factory import get_telethon_client
from app.services.spambot_service import check_account_spambot
from app.services.proxy_service import parse_proxy_line
from app.services.export_service import generate_accounts_excel

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

    status_msg = await message.answer("Загрузка и обработка файла...")
    temp_target = TEMP_DIR / f"{document.file_id}_{filename}"

    try:
        await bot.download(document, destination=temp_target)

        if filename.lower().endswith(".zip"):
            inspect_dir = Path(tempfile.mkdtemp(prefix="bundle_check_"))
            try:
                safe_extract_zip(temp_target, inspect_dir)
                session_files = list(inspect_dir.rglob("*.session"))
                if len(session_files) > 1:
                    proxies_file = next(inspect_dir.rglob("*proxy*.txt"), None)
                    available_proxies = []
                    if proxies_file:
                        for line in proxies_file.read_text(encoding="utf-8").splitlines():
                            parsed_p = parse_proxy_line(line)
                            if parsed_p:
                                available_proxies.append(parsed_p)

                    imported_count = 0
                    errors_count = 0
                    async with async_session_factory() as session:
                        db_proxies = []
                        for host, port, user, pwd, proto in available_proxies:
                            p_obj = Proxy(host=host, port=port, username=user, password=pwd, protocol=proto, is_active=True)
                            session.add(p_obj)
                            db_proxies.append(p_obj)
                        if db_proxies:
                            await session.commit()

                        existing_proxies = (await session.execute(select(Proxy).where(Proxy.is_active == True))).scalars().all()

                        for idx, s_path in enumerate(session_files):
                            chosen_proxy = None
                            proxy_dict = None
                            if existing_proxies:
                                chosen_proxy = existing_proxies[idx % len(existing_proxies)]
                                proxy_dict = {
                                    "proxy_type": chosen_proxy.protocol.lower(),
                                    "addr": chosen_proxy.host,
                                    "port": chosen_proxy.port,
                                    "username": chosen_proxy.username,
                                    "password": chosen_proxy.password,
                                    "rdns": True
                                }

                            ok, s_msg, s_info = await import_session_file(s_path, proxy=proxy_dict)
                            if ok and s_info:
                                existing = (await session.execute(select(Account).where(Account.phone == s_info["phone"]))).scalars().first()
                                if existing:
                                    existing.session_encrypted = s_info["session_encrypted"]
                                    existing.status = "active"
                                    existing.is_active = True
                                    if chosen_proxy:
                                        existing.proxy_id = chosen_proxy.id
                                else:
                                    acc = Account(
                                        phone=s_info["phone"],
                                        session_encrypted=s_info["session_encrypted"],
                                        first_name=s_info.get("first_name"),
                                        last_name=s_info.get("last_name"),
                                        username=s_info.get("username"),
                                        status="active",
                                        is_active=True,
                                        proxy_id=chosen_proxy.id if chosen_proxy else None
                                    )
                                    session.add(acc)
                                imported_count += 1
                            else:
                                errors_count += 1
                        await session.commit()

                    await state.clear()
                    await status_msg.edit_text(
                        f"Пакетный импорт архива завершен.\n"
                        f"Успешно добавлено сессий: {imported_count}\n"
                        f"Ошибок: {errors_count}\n"
                        f"Привязано прокси: {len(db_proxies)} новых",
                        reply_markup=back_keyboard("nav_accounts")
                    )
                    return
            finally:
                shutil.rmtree(inspect_dir, ignore_errors=True)

            success, msg, info = await convert_tdata_archive(temp_target)
            if not success and "2FA" in msg:
                await state.set_state(AccountState.waiting_for_password)
                await state.update_data(archive_path=str(temp_target))
                await status_msg.edit_text("Для этой TData требуется пароль двухэтапной аутентификации. Введите его в ответном сообщении:")
                return

        elif expected_type == "session" or filename.lower().endswith(".session"):
            success, msg, info = await import_session_file(temp_target)
        else:
            await state.clear()
            await status_msg.edit_text("Неподдерживаемый формат файла. Отправьте .zip архив с tdata или .session файл.")
            return

        if not success or not info:
            await state.clear()
            await status_msg.edit_text(f"Не удалось добавить аккаунт: {msg}")
            return

        async with async_session_factory() as session:
            existing = (await session.execute(
                select(Account).where(Account.phone == info["phone"])
            )).scalars().first()

            if existing:
                existing.session_encrypted = info["session_encrypted"]
                existing.first_name = info.get("first_name")
                existing.last_name = info.get("last_name")
                existing.username = info.get("username")
                existing.status = "active"
                existing.is_active = True
                await session.commit()
                response_text = f"Аккаунт {info['phone']} обновлен в базе."
            else:
                acc = Account(
                    phone=info["phone"],
                    session_encrypted=info["session_encrypted"],
                    first_name=info.get("first_name"),
                    last_name=info.get("last_name"),
                    username=info.get("username"),
                    status="active",
                    is_active=True
                )
                session.add(acc)
                await session.commit()
                name_display = info.get('first_name') or info['phone']
                username_display = f" (@{info['username']})" if info.get('username') else ""
                response_text = f"Аккаунт {name_display}{username_display} успешно добавлен в пул."

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
    status_msg = await message.answer("Проверка пароля и конвертация сессии...")

    success, msg, info = await convert_tdata_archive(Path(archive_path), password=password)
    if not success or not info:
        attempts = state_data.get("attempts", 0) + 1
        if attempts >= 3:
            Path(archive_path).unlink(missing_ok=True)
            await state.clear()
            await status_msg.edit_text(
                f"Ошибка авторизации: {msg}\nПревышено количество попыток. Загрузите архив заново.",
                reply_markup=back_keyboard("nav_accounts")
            )
            return

        await state.update_data(attempts=attempts)
        await status_msg.edit_text(
            f"Ошибка авторизации: {msg}\nПопытка {attempts} из 3. Введите пароль еще раз:",
            reply_markup=back_keyboard("nav_accounts")
        )
        return

    try:
        async with async_session_factory() as session:
            existing = (await session.execute(
                select(Account).where(Account.phone == info["phone"])
            )).scalars().first()

            if existing:
                existing.session_encrypted = info["session_encrypted"]
                existing.status = "active"
                await session.commit()
                response_text = f"Аккаунт {info['phone']} успешно обновлен с 2FA паролем."
            else:
                acc = Account(
                    phone=info["phone"],
                    session_encrypted=info["session_encrypted"],
                    first_name=info.get("first_name"),
                    last_name=info.get("last_name"),
                    username=info.get("username"),
                    status="active"
                )
                session.add(acc)
                await session.commit()
                response_text = f"Аккаунт {info['phone']} успешно авторизован и сохранен."

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
        lines.append(f"#{acc.id} {display_name} [{acc.status}] (инвайтов сегодня: {acc.daily_invites_count})")

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
        accounts = (await session.execute(select(Account))).scalars().all()

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
            await status_msg.edit_text(f"Проверено {idx}/{len(accounts)} аккаунтов...\nВалидных: {valid_count}, Недоступных: {banned_count}")

    await status_msg.edit_text(
        f"Проверка завершена.\nВалидных аккаунтов: {valid_count}\nОтозванных/недоступных: {banned_count}",
        reply_markup=back_keyboard("nav_accounts")
    )
    await callback.answer()

@accounts_router.callback_query(F.data == "acc_check_spambot")
async def callback_check_spambot(callback: CallbackQuery):
    status_msg = await callback.message.edit_text("Запуск проверки аккаунтов через @SpamBot...")

    async with async_session_factory() as session:
        accounts = (await session.execute(select(Account).where(Account.status != "banned"))).scalars().all()

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
            await status_msg.edit_text(f"Проверено через @SpamBot {idx}/{len(accounts)}...\nЧистых: {clean_count}, Со спамблоком: {limited_count}")

    await status_msg.edit_text(
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
