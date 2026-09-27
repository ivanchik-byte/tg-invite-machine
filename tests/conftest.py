import os
from pathlib import Path

# Load test env before any app import so Settings() singleton passes validation
_test_env = Path(__file__).parent.parent / ".env.test"
if _test_env.exists():
    for _line in _test_env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.core.config import settings
from app.core.database import Base

@pytest.fixture(autouse=True)
def setup_test_settings():
    test_key = Fernet.generate_key().decode()
    settings.ENCRYPTION_KEY = test_key
    settings.ADMIN_ID = 123456789
    yield

@pytest_asyncio.fixture
async def test_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False
    )

    async with session_factory() as session:
        yield session

    await engine.dispose()
