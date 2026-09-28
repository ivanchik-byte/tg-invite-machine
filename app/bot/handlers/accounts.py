import asyncio
import shutil
import tempfile
from pathlib import Path
from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, Document, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from html import escape as quote_html
from sqlalchemy import select, func, delete
from sqlalchemy.orm import selectinload

from app.core.config import settings, TEMP_DIR
from app.core.database import async_session_factory
from datetime import datetime, timezone
from app.core.security import safe_extract_zip, encrypt_session_string
from app.models.models import Account, Proxy
from app.bot.states import AccountState
from app.bot.keyboards import (
    accounts_menu_keyboard,
    back_keyboard,
    accounts_pagination_keyboard,
    account_view_keyboard,
    account_proxy_pick_keyboard,
)
from app.core.utils import safe_edit_text
from app.telegram.converter import convert_tdata_archive, import_session_file, PASSWORD_REQUIRED
from app.telegram.client_factory import get_telethon_client, decrypt_proxy_password, build_proxy_dict, ProxySecurityError
from app.services.account_service import (
    import_account_bundle,
    register_single_account,
    auto_recover_cooldowns,
    reset_all_cooldowns,
)
from app.services.spambot_service import check_account_spambot
from app.services.proxy_service import (
    import_proxies_from_text,
    bind_proxy_to_account,
    unbind_proxy_from_account,
)
from app.services.export_service import generate_accounts_excel

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB Telegram Bot API hard limit for getFile
MAX_SESSION_FILES = 20

accounts_router = Router()

@accounts_router.callback_query(F.data == "nav_accounts")
async def callback_nav_accounts(callback: CallbackQuery, state: FSMContext | None = None):
    if state is not None:
        await state.clear()
    async with async_session_factory() as session:
        await auto_recover_cooldowns(session)
        count = (await session.execute(select(func.count(Account.id)))).scalar_one()
        active = (await session.execute(
            select(func.count(Account.id)).where(Account.is_active == True, Account.status == "active")
        )).scalar_one()
        cooldown = (await session.execute(
            select(func.count(Account.id)).where(Account.status == "cooldown")
        )).scalar_one()
        banned = (await session.execute(
            select(func.count(Account.id)).where(Account.status == "banned")
        )).scalar_one()

    text = (
        "<b>TG-INVITE-MACHINE | Управление пулом сессий</b>\n"
        "────────────────────────\n"
        f"<b>Всего в базе данных:</b> <code>{count}</code> аккаунтов\n"
        f"• <b>Готовы к работе:</b> <code>{active}</code>\n"
        f"• <b>В режиме отлежки (FloodWait):</b> <code>{cooldown}</code>\n"
        f"• <b>Заблокированы:</b> <code>{banned}</code>\n\n"
        "<b>Способы загрузки:</b>\n"
        "Отправьте документ в чат:\n"
        "• <code>.session</code> - файл сессии Telethon\n"
        "• <code>.zip</code> - архив с папкой <code>tdata</code> Telegram Desktop\n\n"
        "<blockquote>Все сессии хранятся в зашифрованном виде (Fernet). Для TData используется QR-login с изоляцией от десктопа.</blockquote>"
    )
    await safe_edit_text(callback.message, text, reply_markup=accounts_menu_keyboard(has_accounts=count > 0, cooldown_count=cooldown))
    await callback.answer()

@accounts_router.callback_query(F.data == "acc_reset_cooldowns")
async def callback_reset_cooldowns(callback: CallbackQuery, state: FSMContext | None = None):
    async with async_session_factory() as session:
        count = await reset_all_cooldowns(session)
    await callback.answer(f"Сброшена отлежка у {count} аккаунтов. Они снова активны.", show_alert=True)
    await callback_nav_accounts(callback, state)

@accounts_router.callback_query(F.data == "acc_upload_tdata")
async def callback_upload_tdata(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AccountState.waiting_for_file)
    await state.update_data(expected_type="tdata")
    text = (
        "<b>Загрузка аккаунта через TData (Telegram Desktop):</b>\n\n"
        "Отправьте ZIP-архив с папкой <code>tdata</code> документом в этот чат.\n\n"
        "<b>Требования к архиву:</b>\n"
        "• Лимит Telegram Bot API на скачивание ботом: не более <b>20 МБ</b>.\n"
        "• В архиве требуются только файл <code>key_data</code> и 16-значные папки сессии.\n"
        "• Удалите из папки тяжелые кэши (<code>user_data</code>, <code>dumps</code>, <code>webview</code>), чтобы архив весил 1-3 МБ.\n\n"
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
        size_mb = round(document.file_size / (1024 * 1024), 1)
        await state.clear()
        await message.answer(
            f"<b>Файл слишком большой ({size_mb} МБ).</b>\n\n"
            f"Telegram Bot API разрешает ботам скачивать файлы размером не более <b>20 МБ</b>.\n\n"
            f"<b>Как уменьшить размер TData:</b>\n"
            f"• В папке <code>tdata</code> удалите папки кэша: <code>user_data</code>, <code>dumps</code>, <code>webview</code>, <code>emoji</code>.\n"
            f"• Для входа нужны только файл <code>key_data</code> и 16-значные шестнадцатеричные папки сессии.\n"
            f"• Чистый архив TData весит всего 1-3 МБ.",
            reply_markup=back_keyboard("nav_accounts")
        )
        return

    status_msg = await message.answer("Загрузка и обработка файла...")
    temp_target = TEMP_DIR / f"{document.file_id}_{filename}"

    try:
        try:
            await bot.download(document, destination=temp_target)
        except TelegramBadRequest as exc:
            await state.clear()
            if "file is too big" in str(exc).lower():
                await status_msg.edit_text(
                    "<b>[ОШИБКА] Файл превышает лимит Telegram Bot API (20 МБ)</b>\n\n"
                    "Серверы Telegram отклонили скачивание файла из-за ограничения размера в 20 МБ.\n\n"
                    "Очистите кэш (папки <code>user_data</code>, <code>dumps</code>, <code>webview</code>) перед упаковкой в ZIP. Чистый архив TData весит 1-3 МБ.",
                    reply_markup=back_keyboard("nav_accounts")
                )
            else:
                await status_msg.edit_text(
                    f"<b>[ОШИБКА] Сбой при скачивании файла</b>\n\nПричина: {quote_html(str(exc))}",
                    reply_markup=back_keyboard("nav_accounts")
                )
            return
        except Exception as exc:
            await state.clear()
            await status_msg.edit_text(
                f"<b>[ОШИБКА] Не удалось сохранить файл</b>\n\nПричина: {quote_html(str(exc))}",
                reply_markup=back_keyboard("nav_accounts")
            )
            return

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
                    "<b>[ОТКЛОНЕНО] Требуется активный прокси</b>\n\n"
                    "Включена политика Zero-Leak, но в базе нет активных прокси. Добавьте прокси перед импортом архива.",
                    reply_markup=back_keyboard("nav_accounts")
                )
                return

            if bundle_result is not None:
                await state.clear()
                status_tag = "[УСПЕХ]" if bundle_result.imported_count > 0 else "[ОШИБКА]"
                await status_msg.edit_text(
                    f"<b>{status_tag} Пакетный импорт архива завершен</b>\n\n"
                    f"• Успешно добавлено сессий: <code>{bundle_result.imported_count}</code>\n"
                    f"• Ошибок импорта: <code>{bundle_result.errors_count}</code>\n"
                    f"• Добавлено новых прокси: <code>{bundle_result.added_proxies}</code>",
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
                "<b>[ОТКЛОНЕНО] Требуется активный прокси</b>\n\n"
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
                await state.update_data(archive_path=str(temp_target), file_type="zip")
                await status_msg.edit_text(
                    "<b>[ТРЕБУЕТСЯ 2FA] Введите пароль двухэтапной аутентификации</b>\n\n"
                    "Для этого аккаунта включена двухфакторная защита Telegram.\n"
                    "Отправьте ваш облачный пароль (2FA) следующим текстовым сообщением в этот чат:",
                    reply_markup=back_keyboard("nav_accounts")
                )
                return

        elif expected_type == "session" or filename.lower().endswith(".session"):
            success, msg, info = await import_session_file(temp_target, proxy=single_proxy_dict)
            if not success and msg == PASSWORD_REQUIRED:
                await state.set_state(AccountState.waiting_for_password)
                await state.update_data(archive_path=str(temp_target), file_type="session")
                await status_msg.edit_text(
                    "<b>[ТРЕБУЕТСЯ 2FA] Введите пароль двухэтапной аутентификации</b>\n\n"
                    "Для этой сессии включена двухфакторная защита Telegram.\n"
                    "Отправьте ваш пароль (2FA) следующим текстовым сообщением в этот чат:",
                    reply_markup=back_keyboard("nav_accounts")
                )
                return
        else:
            await state.clear()
            await status_msg.edit_text(
                "<b>[ОШИБКА] Неподдерживаемый формат файла</b>\n\n"
                "Отправьте .zip архив с tdata или .session файл.",
                reply_markup=back_keyboard("nav_accounts")
            )
            return

        if not success or not info:
            await state.clear()
            await status_msg.edit_text(
                f"<b>[ОШИБКА] Не удалось добавить аккаунт</b>\n\n"
                f"Причина: <code>{quote_html(msg or 'Неизвестная ошибка конвертации')}</code>\n\n"
                f"Убедитесь, что сессия не отозвана и архив содержит валидные данные авторизации.",
                reply_markup=back_keyboard("nav_accounts")
            )
            return

        async with async_session_factory() as session:
            acc, is_new = await register_single_account(session, info, proxy_id=chosen_proxy_id)
            if is_new:
                name_display = quote_html(info.get('first_name') or info['phone'])
                username_display = f" (@{quote_html(info['username'])})" if info.get('username') else ""
                response_text = (
                    f"<b>[УСПЕХ] Аккаунт успешно добавлен в пул</b>\n\n"
                    f"• Телефон: <code>{quote_html(info['phone'])}</code>\n"
                    f"• Имя: <b>{name_display}</b>{username_display}\n"
                    f"• Статус: активен и готов к работе"
                )
            else:
                response_text = (
                    f"<b>[УСПЕХ] Аккаунт успешно обновлен</b>\n\n"
                    f"• Телефон: <code>{quote_html(info['phone'])}</code>\n"
                    f"• Статус: данные сессии актуализированы в базе"
                )

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
        await message.answer("Файл сессии не найден, повторите загрузку заново.", reply_markup=back_keyboard("nav_accounts"))
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
            "<b>[ОТКЛОНЕНО] Требуется активный прокси</b>\n\n"
            "Включена политика Zero-Leak, но в базе нет активных прокси. Добавьте рабочий прокси перед подтверждением пароля.",
            reply_markup=back_keyboard("nav_accounts")
        )
        return

    proxy_dict = build_proxy_dict(active_proxy) if active_proxy else None
    chosen_proxy_id = active_proxy.id if active_proxy else None
    file_type = state_data.get("file_type", "zip")

    if file_type == "session":
        success, msg, info = await import_session_file(Path(archive_path), password=password, proxy=proxy_dict)
    else:
        success, msg, info = await convert_tdata_archive(Path(archive_path), password=password, proxy=proxy_dict)

    if not success or not info:
        attempts = state_data.get("attempts", 0) + 1
        err_desc = "Неверный пароль 2FA" if msg == PASSWORD_REQUIRED else (msg or "Не удалось подтвердить пароль")
        if attempts >= 3:
            Path(archive_path).unlink(missing_ok=True)
            await state.clear()
            await status_msg.edit_text(
                f"<b>[ОШИБКА] Превышено количество попыток 2FA</b>\n\n"
                f"Причина: <code>{quote_html(err_desc)}</code>\n"
                f"Авторизация отменена. Загрузите файл заново.",
                reply_markup=back_keyboard("nav_accounts")
            )
            return

        await state.update_data(attempts=attempts)
        await status_msg.edit_text(
            f"<b>[ОШИБКА] Неверный пароль 2FA</b>\n\n"
            f"Попытка {attempts} из 3. Проверьте пароль и введите его еще раз в ответном сообщении:",
            reply_markup=back_keyboard("nav_accounts")
        )
        return

    try:
        async with async_session_factory() as session:
            acc, is_new = await register_single_account(
                session,
                info,
                proxy_id=chosen_proxy_id,
                two_fa_password=password
            )
            if is_new:
                response_text = (
                    f"<b>[УСПЕХ] Аккаунт успешно авторизован и добавлен в пул</b>\n\n"
                    f"• Телефон: <code>{quote_html(info['phone'])}</code>\n"
                    f"• 2FA пароль: подтвержден и сохранен\n"
                    f"• Статус: активен и готов к работе"
                )
            else:
                response_text = (
                    f"<b>[УСПЕХ] Аккаунт успешно обновлен</b>\n\n"
                    f"• Телефон: <code>{quote_html(info['phone'])}</code>\n"
                    f"• 2FA пароль: успешно актуализирован"
                )

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

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with async_session_factory() as session:
        await auto_recover_cooldowns(session)
        accounts = (await session.execute(
            select(Account).options(selectinload(Account.proxy)).order_by(Account.id.asc()).offset(offset).limit(limit)
        )).scalars().all()
        total = (await session.execute(select(func.count(Account.id)))).scalar_one()

    if not accounts and offset > 0:
        offset = max(0, offset - limit)
        async with async_session_factory() as session:
            accounts = (await session.execute(
                select(Account).options(selectinload(Account.proxy)).order_by(Account.id.asc()).offset(offset).limit(limit)
            )).scalars().all()

    if not accounts:
        await safe_edit_text(callback.message, "Аккаунты не найдены.", reply_markup=back_keyboard("nav_accounts"))
        await callback.answer()
        return

    page_num = (offset // limit) + 1
    total_pages = max(1, (total + limit - 1) // limit)
    lines = [f"<b>Список аккаунтов</b> (всего: {total}, стр. {page_num}/{total_pages}):\n"]
    account_items = []
    for acc in accounts:
        display_name = acc.username and f"@{acc.username}" or acc.first_name or acc.phone
        if acc.status == "cooldown":
            if acc.cooldown_until and acc.cooldown_until > now:
                rem_sec = int((acc.cooldown_until - now).total_seconds())
                mins = rem_sec // 60
                secs = rem_sec % 60
                status_desc = f"отлежка еще {mins}м {secs}с" if mins > 0 else f"отлежка еще {secs}с"
            else:
                status_desc = "отлежка истекла"
        else:
            status_desc = acc.status

        proxy_desc = f"прокси #{acc.proxy_id}" if acc.proxy_id else "без прокси"
        lines.append(f"• #{acc.id} {quote_html(display_name)} [{status_desc}] ({proxy_desc}, инвайтов: {acc.daily_invites_count})")

        p_mark = f" [P#{acc.proxy_id}]" if acc.proxy_id else ""
        btn_label = f"{display_name}{p_mark}"
        account_items.append((acc.id, btn_label))

    lines.append("\n<i>Нажмите на аккаунт для просмотра, настройки прокси или удаления.</i>")

    await safe_edit_text(
        callback.message,
        "\n".join(lines),
        reply_markup=accounts_pagination_keyboard(offset=offset, limit=limit, total=total, account_items=account_items)
    )
    await callback.answer()

@accounts_router.callback_query(F.data.startswith("acc_view_"))
async def callback_account_view(callback: CallbackQuery):
    parts = callback.data.split("_")
    try:
        acc_id = int(parts[2])
        offset = int(parts[3]) if len(parts) > 3 else 0
    except (ValueError, IndexError):
        await callback.answer("Неверный формат команды.")
        return

    async with async_session_factory() as session:
        await auto_recover_cooldowns(session)
        acc = (await session.execute(
            select(Account).options(selectinload(Account.proxy)).where(Account.id == acc_id)
        )).scalars().first()

    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        callback.data = f"acc_list_{offset}"
        await callback_list_accounts(callback)
        return

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if acc.status == "cooldown":
        if acc.cooldown_until and acc.cooldown_until > now:
            rem_sec = int((acc.cooldown_until - now).total_seconds())
            mins = rem_sec // 60
            secs = rem_sec % 60
            status_desc = f"отлежка еще {mins}м {secs}с" if mins > 0 else f"отлежка еще {secs}с"
        else:
            status_desc = "отлежка истекла"
    else:
        status_desc = acc.status

    if acc.proxy:
        p_status = "[OK] Активен" if acc.proxy.is_active else "[ОШИБКА] Недоступен"
        p_desc = f"<code>#{acc.proxy.id} {acc.proxy.protocol}://{acc.proxy.host}:{acc.proxy.port}</code> ({p_status})"
        has_proxy = True
    else:
        p_desc = "<code>Не привязан (прямое подключение)</code>"
        has_proxy = False

    name_display = quote_html(acc.first_name or "-")
    username_display = f"@{quote_html(acc.username)}" if acc.username else "нет"

    text = (
        f"<b>Параметры аккаунта #{acc.id}</b>\n"
        "────────────────────────\n"
        f"• <b>Телефон:</b> <code>{quote_html(acc.phone)}</code>\n"
        f"• <b>Имя:</b> {name_display}\n"
        f"• <b>Юзернейм:</b> {username_display}\n"
        f"• <b>Статус:</b> <code>{status_desc}</code>\n"
        f"• <b>Инвайтов за сегодня:</b> <code>{acc.daily_invites_count}</code>\n"
        f"• <b>Привязанный прокси:</b>\n  └ {p_desc}\n\n"
        "<blockquote>Для смены или отключения прокси используйте кнопки ниже.</blockquote>"
    )

    await safe_edit_text(
        callback.message,
        text,
        reply_markup=account_view_keyboard(acc_id=acc.id, offset=offset, has_proxy=has_proxy)
    )
    await callback.answer()

@accounts_router.callback_query(F.data.startswith("acc_proxy_pick_"))
async def callback_acc_proxy_pick(callback: CallbackQuery):
    parts = callback.data.split("_")
    try:
        acc_id = int(parts[3])
        offset = int(parts[4]) if len(parts) > 4 else 0
    except (ValueError, IndexError):
        await callback.answer("Неверный формат команды.")
        return

    async with async_session_factory() as session:
        proxies = (await session.execute(
            select(Proxy).options(selectinload(Proxy.accounts)).order_by(Proxy.is_active.desc(), Proxy.id.asc())
        )).scalars().all()

    if not proxies:
        await callback.answer("В базе нет добавленных прокси. Добавьте их в меню Прокси.", show_alert=True)
        return

    items = []
    for p in proxies:
        status_tag = "[OK]" if p.is_active else "[ERR]"
        acc_count = len(p.accounts)
        label = f"#{p.id} {status_tag} {p.host}:{p.port} ({acc_count} акк)"
        items.append((p.id, label))

    text = (
        f"<b>Выбор прокси для аккаунта #{acc_id}</b>\n\n"
        "Выберите прокси из списка доступных в пуле:"
    )
    await safe_edit_text(
        callback.message,
        text,
        reply_markup=account_proxy_pick_keyboard(acc_id=acc_id, offset=offset, proxies=items)
    )
    await callback.answer()

@accounts_router.callback_query(F.data.startswith("acc_proxy_apply_"))
async def callback_acc_proxy_apply(callback: CallbackQuery):
    parts = callback.data.split("_")
    try:
        acc_id = int(parts[3])
        proxy_id = int(parts[4])
        offset = int(parts[5]) if len(parts) > 5 else 0
    except (ValueError, IndexError):
        await callback.answer("Неверный формат команды.")
        return

    async with async_session_factory() as session:
        success = await bind_proxy_to_account(session, proxy_id=proxy_id, account_id=acc_id)

    if success:
        await callback.answer(f"Прокси #{proxy_id} привязан к аккаунту #{acc_id}.", show_alert=True)
    else:
        await callback.answer("Ошибка привязки прокси.", show_alert=True)

    callback.data = f"acc_view_{acc_id}_{offset}"
    await callback_account_view(callback)

@accounts_router.callback_query(F.data.startswith("acc_proxy_detach_"))
async def callback_acc_proxy_detach(callback: CallbackQuery):
    parts = callback.data.split("_")
    try:
        acc_id = int(parts[3])
        offset = int(parts[4]) if len(parts) > 4 else 0
    except (ValueError, IndexError):
        await callback.answer("Неверный формат команды.")
        return

    async with async_session_factory() as session:
        success = await unbind_proxy_from_account(session, account_id=acc_id)

    if success:
        await callback.answer("Прокси успешно отвязан от аккаунта.", show_alert=True)
    else:
        await callback.answer("Аккаунт не найден.", show_alert=True)

    callback.data = f"acc_view_{acc_id}_{offset}"
    await callback_account_view(callback)

@accounts_router.callback_query(F.data.startswith("acc_del_"))
async def callback_delete_account(callback: CallbackQuery):
    parts = callback.data.split("_")
    try:
        acc_id = int(parts[2])
        offset = int(parts[3]) if len(parts) > 3 else 0
    except (ValueError, IndexError):
        await callback.answer("Ошибка формата команды.")
        return

    async with async_session_factory() as session:
        await session.execute(delete(Account).where(Account.id == acc_id))
        await session.commit()

    await callback.answer(f"Аккаунт #{acc_id} удален.")
    callback.data = f"acc_list_{offset}"
    await callback_list_accounts(callback)


@accounts_router.callback_query(F.data == "acc_purge_all_confirm")
async def callback_purge_all_confirm(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Да, удалить все аккаунты", callback_data="acc_purge_all_exec"),
        InlineKeyboardButton(text="Отмена", callback_data="nav_accounts")
    )
    await safe_edit_text(
        callback.message,
        "Вы действительно хотите удалить ВСЕ аккаунты из базы данных?",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@accounts_router.callback_query(F.data == "acc_purge_all_exec")
async def callback_purge_all_exec(callback: CallbackQuery):
    async with async_session_factory() as session:
        result = await session.execute(delete(Account))
        deleted_count = result.rowcount
        await session.commit()

    await safe_edit_text(
        callback.message,
        f"Удалено аккаунтов из базы: {deleted_count} шт.",
        reply_markup=back_keyboard("nav_accounts")
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
        client = get_telethon_client(acc, proxy=acc.proxy)
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
        f"<b>[ИТОГ] Проверка авторизации завершена</b>\n\n"
        f"• Валидных и активных: <code>{valid_count}</code>\n"
        f"• Отозванных или недоступных: <code>{banned_count}</code>",
        reply_markup=back_keyboard("nav_accounts")
    )
    await callback.answer()

@accounts_router.callback_query(F.data == "acc_check_spambot")
async def callback_check_spambot(callback: CallbackQuery):
    await callback.answer("Запуск проверки через @SpamBot...")
    status_msg = await callback.message.edit_text("Запуск проверки аккаунтов через @SpamBot...")

    async with async_session_factory() as session:
        accounts = (await session.execute(
            select(Account).options(selectinload(Account.proxy)).where(Account.status != "banned")
        )).scalars().all()

    clean_count = 0
    limited_count = 0
    outcomes: dict[int, str] = {}

    for idx, acc in enumerate(accounts, 1):
        status, reason = await check_account_spambot(acc)
        outcomes[acc.id] = status
        if status == "active":
            clean_count += 1
        elif status == "spambot":
            limited_count += 1

        if idx % 2 == 0 or idx == len(accounts):
            await safe_edit_text(status_msg, f"Проверено через @SpamBot {idx}/{len(accounts)}...\nЧистых: {clean_count}, Со спамблоком: {limited_count}")

    async with async_session_factory() as session:
        for account_id, status in outcomes.items():
            db_acc = await session.get(Account, account_id)
            if db_acc:
                db_acc.status = status
        await session.commit()

    await safe_edit_text(
        status_msg,
        f"<b>[ИТОГ] Проверка через @SpamBot завершена</b>\n\n"
        f"• Без ограничений (чистые): <code>{clean_count}</code>\n"
        f"• Со спамблоком: <code>{limited_count}</code>",
        reply_markup=back_keyboard("nav_accounts")
    )

@accounts_router.callback_query(F.data == "acc_export_excel")
async def callback_export_excel(callback: CallbackQuery):
    async with async_session_factory() as session:
        accounts = (await session.execute(
            select(Account).options(selectinload(Account.proxy)).order_by(Account.id.asc())
        )).scalars().all()

    if not accounts:
        await callback.answer("В базе нет аккаунтов для выгрузки.", show_alert=True)
        return

    # offload openpyxl zip packaging to worker thread to prevent event loop stalls
    excel_file = await asyncio.to_thread(generate_accounts_excel, accounts)
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
