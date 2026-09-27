import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.core.config import settings
from app.core.database import init_db, engine
from app.bot.middlewares.auth import AdminOnlyMiddleware
from app.bot.handlers.menu import menu_router
from app.bot.handlers.accounts import accounts_router
from app.bot.handlers.proxies import proxies_router
from app.bot.handlers.parser import parser_router
from app.bot.handlers.inviter import inviter_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("tg_invite_machine")

async def main():
    if not settings.BOT_TOKEN:
        logger.error("BOT_TOKEN не задан. Укажите токен бота в файле .env")
        sys.exit(1)

    logger.info("Инициализация базы данных...")
    await init_db()

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher(storage=MemoryStorage())

    admin_middleware = AdminOnlyMiddleware()
    dp.message.outer_middleware(admin_middleware)
    dp.callback_query.outer_middleware(admin_middleware)

    dp.include_router(menu_router)
    dp.include_router(accounts_router)
    dp.include_router(proxies_router)
    dp.include_router(parser_router)
    dp.include_router(inviter_router)

    logger.info("Бот запущен и ожидает обновлений...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        try:
            await bot.set_my_commands([
                BotCommand(command="start", description="Главное меню управления"),
                BotCommand(command="menu", description="Открыть панель разделов"),
                BotCommand(command="stats", description="Сводная статистика системы"),
                BotCommand(command="help", description="Инструкция и справка"),
                BotCommand(command="cancel", description="Отменить текущий ввод")
            ])
        except Exception as exc:
            logger.warning("Не удалось зарегистрировать команды бота: %s", exc)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await engine.dispose()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
