import pytest
from unittest.mock import AsyncMock, MagicMock
from aiogram.types import User
from app.core.config import settings
from app.bot.middlewares.auth import AdminOnlyMiddleware

@pytest.mark.asyncio
async def test_admin_only_middleware_allows_admin():
    settings.ADMIN_ID = 777
    middleware = AdminOnlyMiddleware()
    handler = AsyncMock(return_value="success")

    user = MagicMock(spec=User)
    user.id = 777
    data = {"event_from_user": user}

    result = await middleware(handler, MagicMock(), data)
    assert result == "success"
    handler.assert_awaited_once()

@pytest.mark.asyncio
async def test_admin_only_middleware_blocks_stranger():
    settings.ADMIN_ID = 777
    middleware = AdminOnlyMiddleware()
    handler = AsyncMock()

    user = MagicMock(spec=User)
    user.id = 999
    data = {"event_from_user": user}

    result = await middleware(handler, MagicMock(), data)
    assert result is None
    handler.assert_not_awaited()

@pytest.mark.asyncio
async def test_admin_only_middleware_blocks_when_admin_not_configured():
    settings.ADMIN_ID = 0
    middleware = AdminOnlyMiddleware()
    handler = AsyncMock()

    user = MagicMock(spec=User)
    user.id = 123
    data = {"event_from_user": user}

    result = await middleware(handler, MagicMock(), data)
    assert result is None
    handler.assert_not_awaited()
