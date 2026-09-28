import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.models import Base
from app.services.proxy_service import import_proxies_from_text


@pytest.mark.asyncio
async def test_proxy_dedup_distinguishes_protocol_and_user():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        added, _ = await import_proxies_from_text(session, "10.0.0.1:1080:user1:pass1\n10.0.0.1:1080:user2:pass2")
        assert added == 2

    async with factory() as session:
        added, skipped = await import_proxies_from_text(session, "10.0.0.1:1080:user1:pass1")
        assert added == 0
        assert skipped == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_proxy_dedup_same_endpoint_twice_in_batch():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        added, skipped = await import_proxies_from_text(
            session, "10.0.0.2:1080:user1:pass1\n10.0.0.2:1080:user1:pass1"
        )
        assert added == 1
        assert skipped == 1
    await engine.dispose()


def test_dashboard_reexport_keeps_handler_imports_working():
    from app.bot.handlers.menu import build_main_dashboard_text, build_stats_text
    from app.bot import dashboard
    assert dashboard.build_main_dashboard_text is build_main_dashboard_text
    assert dashboard.build_stats_text is build_stats_text


def test_task_manager_stop_keeps_handle_until_done():
    import asyncio
    from app.services.task_manager import InviteTaskManager

    async def scenario():
        manager = InviteTaskManager()

        async def worker():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise

        orch = type("Orch", (), {"stop": lambda self: None})()
        manager.start(7, orch, worker())
        assert manager.is_running()
        assert manager.stop() is True
        await asyncio.sleep(0.05)
        assert manager.active_task_handle is None
        assert manager.active_task_id is None

    asyncio.run(scenario())
