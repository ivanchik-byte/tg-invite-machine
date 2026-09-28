from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select, func

from app.core.config import settings
from app.core.database import async_session_factory
from app.models.models import Proxy, Account
from app.bot.states import ProxyState
from app.bot.keyboards import proxies_menu_keyboard, back_keyboard
from app.services.proxy_service import import_proxies_from_text, check_proxy_reachability, auto_assign_proxies
from app.telegram.client_factory import decrypt_proxy_password
from app.core.utils import safe_edit_text

proxies_router = Router()

@proxies_router.callback_query(F.data == "nav_proxies")
async def callback_nav_proxies(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with async_session_factory() as session:
        total = (await session.execute(select(func.count(Proxy.id)))).scalar_one()
        active = (await session.execute(select(func.count(Proxy.id)).where(Proxy.is_active == True))).scalar_one()
        active_accounts = (await session.execute(
            select(func.count(Account.id)).where(Account.is_active == True, Account.status == "active")
        )).scalar_one()

    policy_label = "Строгая изоляция (Zero-Leak)" if settings.REQUIRE_STRICT_PROXIES else "Тестовый режим (разрешено прямое IP)"

    text = (
        "<b>TG-INVITE-MACHINE | Сетевой контур (Прокси)</b>\n"
        "────────────────────────\n"
        f"<b>Статус пула:</b> <code>[ OK ] {active} из {total} активны</code>\n"
        f"• <b>Доступно для сессий:</b> <code>{active}</code> шт.\n"
        f"• <b>Ошибки подключения:</b> <code>{total - active}</code> шт.\n"
        f"• <b>Активных сессий в пуле:</b> <code>{active_accounts}</code> шт.\n"
        f"• <b>Политика безопасности:</b> <code>{policy_label}</code>\n\n"
        "<b>Синтаксис для добавления списком:</b>\n"
        "<code>host:port:user:pass</code>\n"
        "<code>host:port</code>\n"
        "<code>socks5://user:pass@host:port</code>\n"
        "<code>http://user:pass@host:port</code>\n\n"
        "<blockquote>Рекомендуется использовать индивидуальный SOCKS5 IPv4 адрес для каждой сессии. Пароли шифруются.</blockquote>"
    )
    await safe_edit_text(callback.message, text, reply_markup=proxies_menu_keyboard())
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
    status_msg = await callback.message.edit_text("Проверка доступности всех прокси по TCP...")

    async with async_session_factory() as session:
        proxies = (await session.execute(select(Proxy))).scalars().all()

    if not proxies:
        await safe_edit_text(
            status_msg,
            "<b>[ВНИМАНИЕ] Список прокси пуст</b>\n\nДобавьте прокси через меню.",
            reply_markup=back_keyboard("nav_proxies")
        )
        await callback.answer()
        return

    working = 0
    failed = 0

    for idx, proxy in enumerate(proxies, 1):
        pwd = decrypt_proxy_password(proxy.password) if proxy.password else None
        is_ok, err = await check_proxy_reachability(
            host=proxy.host,
            port=proxy.port,
            protocol=proxy.protocol,
            username=proxy.username,
            password=pwd,
            timeout_seconds=7.0
        )
        async with async_session_factory() as session:
            db_proxy = await session.get(Proxy, proxy.id)
            if db_proxy:
                db_proxy.is_active = is_ok
                db_proxy.last_error = err
                await session.commit()

        if is_ok:
            working += 1
        else:
            failed += 1

        if idx % 5 == 0 or idx == len(proxies):
            await safe_edit_text(status_msg, f"Проверено {idx}/{len(proxies)} прокси...\nДоступно: {working}, Недоступно: {failed}")

    status_tag = "[УСПЕХ]" if failed == 0 and working > 0 else ("[ОШИБКА]" if working == 0 else "[ИТОГ]")
    text = (
        f"<b>{status_tag} Проверка прокси завершена</b>\n\n"
        f"• Доступных (активны): <code>{working}</code>\n"
        f"• Недоступных (ошибки): <code>{failed}</code>"
    )
    await safe_edit_text(status_msg, text, reply_markup=back_keyboard("nav_proxies"))
    await callback.answer()

@proxies_router.callback_query(F.data == "proxy_auto_bind")
async def callback_proxy_auto_bind(callback: CallbackQuery):
    async with async_session_factory() as session:
        assigned = await auto_assign_proxies(session)
    await callback.answer(f"Привязано прокси к {assigned} аккаунтам.", show_alert=True)
    await callback_nav_proxies(callback, None)
