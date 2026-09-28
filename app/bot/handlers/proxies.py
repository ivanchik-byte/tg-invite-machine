import asyncio
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select, func

from html import escape as quote_html
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import async_session_factory
from app.models.models import Proxy, Account
from app.bot.states import ProxyState
from app.bot.keyboards import (
    proxies_menu_keyboard,
    back_keyboard,
    proxy_pagination_keyboard,
    proxy_view_keyboard,
    proxy_bind_account_select_keyboard,
    proxy_purge_dead_keyboard,
    proxy_purge_all_keyboard,
)
from app.services.proxy_service import (
    import_proxies_from_text,
    check_proxy_reachability,
    auto_assign_proxies,
    delete_single_proxy,
    purge_dead_proxies,
    purge_all_proxies,
    bind_proxy_to_account,
    unbind_all_from_proxy,
)
from app.telegram.client_factory import decrypt_proxy_password
from app.core.utils import safe_edit_text

proxies_router = Router()

@proxies_router.callback_query(F.data == "nav_proxies")
async def callback_nav_proxies(callback: CallbackQuery, state: FSMContext | None = None):
    if state is not None:
        await state.clear()
    async with async_session_factory() as session:
        total = (await session.execute(select(func.count(Proxy.id)))).scalar_one()
        active = (await session.execute(select(func.count(Proxy.id)).where(Proxy.is_active == True))).scalar_one()
        active_accounts = (await session.execute(
            select(func.count(Account.id)).where(Account.is_active == True, Account.status == "active")
        )).scalar_one()

    policy_label = "Строгая изоляция (Zero-Leak)" if settings.REQUIRE_STRICT_PROXIES else "Тестовый режим (разрешено прямое IP)"
    dead_count = max(0, total - active)

    text = (
        "<b>TG-INVITE-MACHINE | Сетевой контур (Прокси)</b>\n"
        "────────────────────────\n"
        f"<b>Статус пула:</b> <code>[ OK ] {active} из {total} активны</code>\n"
        f"• <b>Доступно для сессий:</b> <code>{active}</code> шт.\n"
        f"• <b>Ошибки подключения:</b> <code>{dead_count}</code> шт.\n"
        f"• <b>Активных сессий в пуле:</b> <code>{active_accounts}</code> шт.\n"
        f"• <b>Политика безопасности:</b> <code>{policy_label}</code>\n\n"
        "<b>Синтаксис для добавления списком:</b>\n"
        "<code>host:port:user:pass</code>\n"
        "<code>host:port</code>\n"
        "<code>socks5://user:pass@host:port</code>\n"
        "<code>http://user:pass@host:port</code>\n\n"
        "<blockquote>Рекомендуется использовать индивидуальный SOCKS5 IPv4 адрес для каждой сессии. Пароли шифруются.</blockquote>"
    )
    await safe_edit_text(callback.message, text, reply_markup=proxies_menu_keyboard(has_proxies=total > 0, dead_count=dead_count))
    await callback.answer()


@proxies_router.callback_query(F.data == "proxy_add")
async def callback_proxy_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProxyState.waiting_for_input)
    text = (
        "<b>Добавление прокси списком</b>\n"
        "────────────────────────\n"
        "Отправьте список прокси текстом (каждый с новой строки):\n\n"
        "<i>Пример форматов:</i>\n"
        "<code>192.168.1.100:8000:proxyuser:strongpass</code>\n"
        "<code>192.168.1.101:8080</code>\n"
        "<code>socks5://user:pass@192.168.1.102:1080</code>\n\n"
        "<i>Для отмены операции отправьте /cancel или нажмите «Назад».</i>"
    )
    await safe_edit_text(callback.message, text, reply_markup=back_keyboard("nav_proxies"))
    await callback.answer()

@proxies_router.message(ProxyState.waiting_for_input, F.text)
async def handle_proxy_input(message: Message, state: FSMContext):
    raw_text = message.text.strip()
    status_msg = await message.answer("Импорт и валидация прокси...")

    try:
        async with async_session_factory() as session:
            added, skipped = await import_proxies_from_text(session, raw_text)

        status_tag = "[УСПЕХ]" if added > 0 else "[ВНИМАНИЕ]"
        text = (
            f"<b>{status_tag} Результат импорта прокси</b>\n\n"
            f"• Успешно добавлено новых: <code>{added}</code>\n"
            f"• Пропущено дубликатов или некорректных: <code>{skipped}</code>"
        )
        await status_msg.edit_text(text, reply_markup=back_keyboard("nav_proxies"))
    finally:
        await state.clear()

@proxies_router.callback_query(F.data == "proxy_check")
async def callback_proxy_check(callback: CallbackQuery):
    await safe_edit_text(callback.message, "Проверка доступности всех прокси по TCP...")

    async with async_session_factory() as session:
        stored = (await session.execute(select(Proxy))).scalars().all()
        entries = [
            (p.id, p.host, p.port, p.protocol, p.username, p.password)
            for p in stored
        ]

    if not entries:
        await safe_edit_text(
            callback.message,
            "<b>[ВНИМАНИЕ] Список прокси пуст</b>\n\nДобавьте прокси через меню.",
            reply_markup=back_keyboard("nav_proxies")
        )
        await callback.answer()
        return

    # bounded probe concurrency: 10 parallel sockets prevent hitting local fd limits
    semaphore = asyncio.Semaphore(10)

    async def probe(entry):
        proxy_id, host, port, protocol, username, password_enc = entry
        try:
            pwd = decrypt_proxy_password(password_enc) if password_enc else None
        except ValueError as exc:
            return proxy_id, False, f"proxy password decrypt failed: {exc}"
        async with semaphore:
            return proxy_id, *await check_proxy_reachability(
                host=host,
                port=port,
                protocol=protocol,
                username=username,
                password=pwd,
                timeout_seconds=7.0
            )

    working = 0
    failed = 0
    outcomes = []
    checked = 0
    for coro in asyncio.as_completed([probe(e) for e in entries]):
        proxy_id, is_ok, err = await coro
        outcomes.append((proxy_id, is_ok, err))
        checked += 1
        if is_ok:
            working += 1
        else:
            failed += 1
        if checked % 5 == 0 or checked == len(entries):
            await safe_edit_text(callback.message, f"Проверено {checked}/{len(entries)} прокси...\nДоступно: {working}, Недоступно: {failed}")

    async with async_session_factory() as session:
        for proxy_id, is_ok, err in outcomes:
            db_proxy = await session.get(Proxy, proxy_id)
            if db_proxy:
                db_proxy.is_active = is_ok
                db_proxy.last_error = err
        await session.commit()

    status_tag = "[УСПЕХ]" if failed == 0 and working > 0 else ("[ОШИБКА]" if working == 0 else "[ИТОГ]")
    text = (
        f"<b>{status_tag} Проверка прокси завершена</b>\n\n"
        f"• Доступных (активны): <code>{working}</code>\n"
        f"• Недоступных (ошибки): <code>{failed}</code>"
    )
    await safe_edit_text(callback.message, text, reply_markup=back_keyboard("nav_proxies"))
    await callback.answer()

@proxies_router.callback_query(F.data == "proxy_auto_bind")
async def callback_proxy_auto_bind(callback: CallbackQuery):
    async with async_session_factory() as session:
        assigned = await auto_assign_proxies(session)
    await callback.answer(f"Привязано прокси к {assigned} аккаунтам.", show_alert=True)
    await callback_nav_proxies(callback, None)

@proxies_router.callback_query(F.data.startswith("proxy_list_"))
async def callback_proxy_list(callback: CallbackQuery):
    try:
        offset = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        offset = 0

    limit = 8
    async with async_session_factory() as session:
        proxies = (await session.execute(
            select(Proxy).options(selectinload(Proxy.accounts)).order_by(Proxy.id.asc()).offset(offset).limit(limit)
        )).scalars().all()
        total = (await session.execute(select(func.count(Proxy.id)))).scalar_one()

    if not proxies and offset > 0:
        offset = max(0, offset - limit)
        async with async_session_factory() as session:
            proxies = (await session.execute(
                select(Proxy).options(selectinload(Proxy.accounts)).order_by(Proxy.id.asc()).offset(offset).limit(limit)
            )).scalars().all()

    if not proxies:
        await safe_edit_text(callback.message, "Список прокси пуст.", reply_markup=back_keyboard("nav_proxies"))
        await callback.answer()
        return

    page_num = (offset // limit) + 1
    total_pages = max(1, (total + limit - 1) // limit)
    lines = [f"<b>Список прокси</b> (всего: {total}, стр. {page_num}/{total_pages}):\n"]

    proxy_items = []
    for p in proxies:
        status_tag = "[OK]" if p.is_active else "[ERR]"
        acc_count = len(p.accounts)
        label = f"#{p.id} {status_tag} {p.host}:{p.port} ({acc_count} акк)"
        proxy_items.append((p.id, label))

        auth_desc = "auth" if (p.username or p.password) else "no auth"
        err_desc = f"\n  └ <i>{quote_html(p.last_error[:50])}</i>" if p.last_error and not p.is_active else ""
        lines.append(f"• #{p.id} <code>{p.protocol}://{p.host}:{p.port}</code> [{status_tag}] ({acc_count} акк, {auth_desc}){err_desc}")

    lines.append("\n<i>Нажмите на кнопку с прокси для детального просмотра и настройки.</i>")

    await safe_edit_text(
        callback.message,
        "\n".join(lines),
        reply_markup=proxy_pagination_keyboard(offset=offset, limit=limit, total=total, proxy_items=proxy_items)
    )
    await callback.answer()

@proxies_router.callback_query(F.data.startswith("proxy_view_"))
async def callback_proxy_view(callback: CallbackQuery):
    parts = callback.data.split("_")
    try:
        proxy_id = int(parts[2])
        offset = int(parts[3]) if len(parts) > 3 else 0
    except (ValueError, IndexError):
        await callback.answer("Неверный формат команды.")
        return

    async with async_session_factory() as session:
        proxy = (await session.execute(
            select(Proxy).options(selectinload(Proxy.accounts)).where(Proxy.id == proxy_id)
        )).scalars().first()

    if not proxy:
        await callback.answer("Прокси не найден.", show_alert=True)
        callback.data = f"proxy_list_{offset}"
        await callback_proxy_list(callback)
        return

    status_tag = "[OK] Активен" if proxy.is_active else "[ОШИБКА] Недоступен"
    auth_tag = f"Логин: <code>{quote_html(proxy.username)}</code>" if proxy.username else "Без авторизации"
    err_text = f"<code>{quote_html(proxy.last_error)}</code>" if proxy.last_error else "Нет"

    if proxy.accounts:
        acc_lines = [f"• #{a.id} <code>{quote_html(a.phone)}</code> (@{quote_html(a.username or 'нет')})" for a in proxy.accounts]
        bound_desc = "\n".join(acc_lines)
    else:
        bound_desc = "• Свободен (нет привязанных аккаунтов)"

    text = (
        f"<b>Параметры прокси #{proxy.id}</b>\n"
        "────────────────────────\n"
        f"• <b>Адрес:</b> <code>{proxy.protocol}://{proxy.host}:{proxy.port}</code>\n"
        f"• <b>Протокол:</b> <code>{proxy.protocol.upper()}</code>\n"
        f"• <b>Авторизация:</b> {auth_tag}\n"
        f"• <b>Статус соединения:</b> <code>{status_tag}</code>\n"
        f"• <b>Последняя ошибка:</b> {err_text}\n\n"
        f"<b>Привязанные аккаунты ({len(proxy.accounts)}):</b>\n"
        f"{bound_desc}"
    )

    await safe_edit_text(
        callback.message,
        text,
        reply_markup=proxy_view_keyboard(proxy_id=proxy.id, offset=offset, has_accounts=len(proxy.accounts) > 0)
    )
    await callback.answer()

@proxies_router.callback_query(F.data.startswith("proxy_del_"))
async def callback_proxy_del(callback: CallbackQuery):
    parts = callback.data.split("_")
    try:
        proxy_id = int(parts[2])
        offset = int(parts[3]) if len(parts) > 3 else 0
    except (ValueError, IndexError):
        await callback.answer("Неверный формат команды.")
        return

    async with async_session_factory() as session:
        success = await delete_single_proxy(session, proxy_id)

    if success:
        await callback.answer(f"Прокси #{proxy_id} удален из пула.", show_alert=True)
    else:
        await callback.answer("Прокси не найден.", show_alert=True)

    callback.data = f"proxy_list_{offset}"
    await callback_proxy_list(callback)

@proxies_router.callback_query(F.data.startswith("proxy_probe_"))
async def callback_proxy_probe(callback: CallbackQuery):
    parts = callback.data.split("_")
    try:
        proxy_id = int(parts[2])
        offset = int(parts[3]) if len(parts) > 3 else 0
    except (ValueError, IndexError):
        await callback.answer("Неверный формат команды.")
        return

    async with async_session_factory() as session:
        proxy = await session.get(Proxy, proxy_id)
        if not proxy:
            await callback.answer("Прокси не найден.", show_alert=True)
            return
        host, port, protocol, username, enc_pwd = proxy.host, proxy.port, proxy.protocol, proxy.username, proxy.password

    pwd = None
    if enc_pwd:
        try:
            pwd = decrypt_proxy_password(enc_pwd)
        except Exception:
            pwd = None

    is_ok, err = await check_proxy_reachability(
        host=host,
        port=port,
        protocol=protocol,
        username=username,
        password=pwd,
        timeout_seconds=7.0
    )

    async with async_session_factory() as session:
        p = await session.get(Proxy, proxy_id)
        if p:
            p.is_active = is_ok
            p.last_error = err
            await session.commit()

    res_badge = "[OK] Доступен" if is_ok else f"[ERR] {err}"
    await callback.answer(f"Результат проверки: {res_badge}", show_alert=True)
    callback.data = f"proxy_view_{proxy_id}_{offset}"
    await callback_proxy_view(callback)

@proxies_router.callback_query(F.data.startswith("proxy_bind_pick_"))
async def callback_proxy_bind_pick(callback: CallbackQuery):
    parts = callback.data.split("_")
    try:
        proxy_id = int(parts[3])
        offset = int(parts[4]) if len(parts) > 4 else 0
    except (ValueError, IndexError):
        await callback.answer("Неверный формат команды.")
        return

    async with async_session_factory() as session:
        accounts = (await session.execute(
            select(Account).order_by(Account.id.asc())
        )).scalars().all()

    if not accounts:
        await callback.answer("В базе нет аккаунтов для привязки.", show_alert=True)
        return

    items = []
    for a in accounts:
        bound_marker = f"(прокси #{a.proxy_id})" if a.proxy_id else "(без прокси)"
        name = a.username and f"@{a.username}" or a.phone
        items.append((a.id, f"#{a.id} {name} {bound_marker}"))

    text = (
        f"<b>Привязка аккаунта к прокси #{proxy_id}</b>\n\n"
        "Выберите аккаунт из списка ниже для привязки к данному прокси:"
    )
    await safe_edit_text(
        callback.message,
        text,
        reply_markup=proxy_bind_account_select_keyboard(proxy_id=proxy_id, offset=offset, accounts=items)
    )
    await callback.answer()

@proxies_router.callback_query(F.data.startswith("proxy_bind_set_"))
async def callback_proxy_bind_set(callback: CallbackQuery):
    parts = callback.data.split("_")
    try:
        proxy_id = int(parts[3])
        acc_id = int(parts[4])
        offset = int(parts[5]) if len(parts) > 5 else 0
    except (ValueError, IndexError):
        await callback.answer("Неверный формат команды.")
        return

    async with async_session_factory() as session:
        success = await bind_proxy_to_account(session, proxy_id=proxy_id, account_id=acc_id)

    if success:
        await callback.answer(f"Аккаунт #{acc_id} привязан к прокси #{proxy_id}.", show_alert=True)
    else:
        await callback.answer("Ошибка привязки аккаунта.", show_alert=True)

    callback.data = f"proxy_view_{proxy_id}_{offset}"
    await callback_proxy_view(callback)

@proxies_router.callback_query(F.data.startswith("proxy_unbind_"))
async def callback_proxy_unbind(callback: CallbackQuery):
    parts = callback.data.split("_")
    try:
        proxy_id = int(parts[2])
        offset = int(parts[3]) if len(parts) > 3 else 0
    except (ValueError, IndexError):
        await callback.answer("Неверный формат команды.")
        return

    async with async_session_factory() as session:
        unbound_count = await unbind_all_from_proxy(session, proxy_id=proxy_id)

    await callback.answer(f"Отвязано {unbound_count} аккаунтов от прокси #{proxy_id}.", show_alert=True)
    callback.data = f"proxy_view_{proxy_id}_{offset}"
    await callback_proxy_view(callback)

@proxies_router.callback_query(F.data == "proxy_purge_dead_confirm")
async def callback_proxy_purge_dead_confirm(callback: CallbackQuery):
    async with async_session_factory() as session:
        dead_count = (await session.execute(
            select(func.count(Proxy.id)).where(Proxy.is_active == False)
        )).scalar_one()

    if dead_count == 0:
        await callback.answer("В пуле нет нерабочих прокси.", show_alert=True)
        return

    text = (
        "<b>Очистка нерабочих прокси</b>\n"
        "────────────────────────\n"
        f"Найдено нерабочих прокси: <code>{dead_count}</code> шт.\n\n"
        "Вы действительно хотите удалить все нерабочие прокси из базы?\n"
        "Привязанные к ним аккаунты останутся в базе и будут переведены в статус без прокси."
    )
    await safe_edit_text(callback.message, text, reply_markup=proxy_purge_dead_keyboard())
    await callback.answer()

@proxies_router.callback_query(F.data == "proxy_purge_dead_exec")
async def callback_proxy_purge_dead_exec(callback: CallbackQuery):
    async with async_session_factory() as session:
        count = await purge_dead_proxies(session)

    await callback.answer(f"Удалено {count} нерабочих прокси.", show_alert=True)
    await callback_nav_proxies(callback, None)

@proxies_router.callback_query(F.data == "proxy_purge_all_confirm")
async def callback_proxy_purge_all_confirm(callback: CallbackQuery):
    async with async_session_factory() as session:
        total_count = (await session.execute(select(func.count(Proxy.id)))).scalar_one()

    if total_count == 0:
        await callback.answer("Список прокси уже пуст.", show_alert=True)
        return

    text = (
        "<b>Удаление ВСЕХ прокси</b>\n"
        "────────────────────────\n"
        f"Всего прокси в базе: <code>{total_count}</code> шт.\n\n"
        "Вы уверены, что хотите удалить ВСЕ прокси из базы данных?\n"
        "Все аккаунты будут отвязаны от прокси."
    )
    await safe_edit_text(callback.message, text, reply_markup=proxy_purge_all_keyboard())
    await callback.answer()

@proxies_router.callback_query(F.data == "proxy_purge_all_exec")
async def callback_proxy_purge_all_exec(callback: CallbackQuery):
    async with async_session_factory() as session:
        count = await purge_all_proxies(session)

    await callback.answer(f"Удалено {count} прокси из базы.", show_alert=True)
    await callback_nav_proxies(callback, None)

