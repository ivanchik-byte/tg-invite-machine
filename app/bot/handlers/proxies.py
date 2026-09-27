from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select, func

from app.core.database import async_session_factory
from app.models.models import Proxy
from app.bot.states import ProxyState
from app.bot.keyboards import proxies_menu_keyboard, back_keyboard
from app.services.proxy_service import import_proxies_from_text, check_proxy_reachability
from app.core.utils import safe_edit_text

proxies_router = Router()

@proxies_router.callback_query(F.data == "nav_proxies")
async def callback_nav_proxies(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with async_session_factory() as session:
        total = (await session.execute(select(func.count(Proxy.id)))).scalar_one()
        active = (await session.execute(select(func.count(Proxy.id)).where(Proxy.is_active == True))).scalar_one()

    text = (
        "Управление сетевыми прокси (SOCKS5 / HTTP).\n\n"
        f"Всего в пуле: {total}\n"
        f"Активных: {active}\n\n"
        "Поддерживаются форматы:\n"
        "host:port:user:pass\n"
        "host:port\n"
        "socks5://user:pass@host:port"
    )
    await safe_edit_text(callback.message, text, reply_markup=proxies_menu_keyboard())
    await callback.answer()

@proxies_router.callback_query(F.data == "proxy_add")
async def callback_proxy_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProxyState.waiting_for_input)
    text = (
        "Отправьте список прокси текстом в ответном сообщении (каждый с новой строки):"
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

        text = (
            f"Результат импорта прокси:\n"
            f"Добавлено новых: {added}\n"
            f"Пропущено дублей или некорректных: {skipped}"
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
        await safe_edit_text(status_msg, "Список прокси пуст.", reply_markup=back_keyboard("nav_proxies"))
        await callback.answer()
        return

    working = 0
    failed = 0

    for idx, proxy in enumerate(proxies, 1):
        is_ok, err = await check_proxy_reachability(proxy.host, proxy.port)
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

    text = (
        f"Проверка прокси завершена.\n"
        f"Доступных: {working}\n"
        f"Недоступных: {failed}"
    )
    await safe_edit_text(status_msg, text, reply_markup=back_keyboard("nav_proxies"))
    await callback.answer()
