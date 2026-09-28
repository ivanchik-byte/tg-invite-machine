from collections.abc import AsyncGenerator
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings

class Base(DeclarativeBase):
    pass

engine_kwargs = {"echo": False, "future": True}
if "sqlite" in settings.DATABASE_URL:
    engine_kwargs["connect_args"] = {"timeout": 15}

engine = create_async_engine(settings.DATABASE_URL, **engine_kwargs)

if "sqlite" in settings.DATABASE_URL:
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=15000;")
        cursor.close()


async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session

async def init_db() -> None:
    from app.models import models  # noqa: F401
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        if "sqlite" in settings.DATABASE_URL:
            def run_sqlite_migrations(conn):
                from sqlalchemy import text
                account_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(accounts)")).fetchall()]
                if account_cols:
                    if "two_fa_password" not in account_cols:
                        conn.execute(text("ALTER TABLE accounts ADD COLUMN two_fa_password TEXT;"))
                    if "api_id" not in account_cols:
                        conn.execute(text("ALTER TABLE accounts ADD COLUMN api_id INTEGER;"))
                    if "api_hash" not in account_cols:
                        conn.execute(text("ALTER TABLE accounts ADD COLUMN api_hash VARCHAR(64);"))
                    if "last_attempt_at" not in account_cols:
                        conn.execute(text("ALTER TABLE accounts ADD COLUMN last_attempt_at DATETIME;"))
                task_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(invite_tasks)")).fetchall()]
                if task_cols:
                    if "max_invites" not in task_cols:
                        conn.execute(text("ALTER TABLE invite_tasks ADD COLUMN max_invites INTEGER;"))
                    if "concurrency_mode" not in task_cols:
                        conn.execute(text("ALTER TABLE invite_tasks ADD COLUMN concurrency_mode VARCHAR(32) DEFAULT 'sequential';"))
                aud_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(audience_members)")).fetchall()]
                if aud_cols:
                    if "access_hash" not in aud_cols:
                        conn.execute(text("ALTER TABLE audience_members ADD COLUMN access_hash BIGINT;"))
                group_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(target_groups)")).fetchall()]
                if group_cols:
                    if "chat_type" not in group_cols:
                        conn.execute(text("ALTER TABLE target_groups ADD COLUMN chat_type VARCHAR(16);"))
                aud_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(audience_members)")).fetchall()]
                if aud_cols:
                    if "last_seen_at" not in aud_cols:
                        conn.execute(text("ALTER TABLE audience_members ADD COLUMN last_seen_at DATETIME;"))
            await connection.run_sync(run_sqlite_migrations)
        else:
            def run_pg_migrations(conn):
                from sqlalchemy import text
                conn.execute(text("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMP;"))
                conn.execute(text("ALTER TABLE audience_members ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP;"))
                conn.execute(text("ALTER TABLE invite_tasks ADD COLUMN IF NOT EXISTS concurrency_mode VARCHAR(32) DEFAULT 'sequential';"))
            await connection.run_sync(run_pg_migrations)
