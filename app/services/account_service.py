import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Dict, Any, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import safe_extract_zip
from app.models.models import Account, Proxy
from app.services.proxy_service import import_proxies_from_text
from app.telegram.client_factory import build_proxy_dict, ProxySecurityError
from app.telegram.converter import import_session_file

@dataclass
class ImportBundleResult:
    imported_count: int
    errors_count: int
    added_proxies: int
    is_session_bundle: bool = True

async def register_single_account(
    session: AsyncSession,
    info: Dict[str, Any],
    proxy_id: Optional[int] = None
) -> Tuple[Account, bool]:
    phone = info["phone"]
    existing = (await session.execute(
        select(Account).where(Account.phone == phone)
    )).scalars().first()

    if existing:
        existing.session_encrypted = info["session_encrypted"]
        existing.first_name = info.get("first_name")
        existing.last_name = info.get("last_name")
        existing.username = info.get("username")
        existing.status = "active"
        existing.is_active = True
        if proxy_id:
            existing.proxy_id = proxy_id
        await session.commit()
        return existing, False

    acc = Account(
        phone=phone,
        session_encrypted=info["session_encrypted"],
        first_name=info.get("first_name"),
        last_name=info.get("last_name"),
        username=info.get("username"),
        status="active",
        is_active=True,
        proxy_id=proxy_id
    )
    session.add(acc)
    await session.commit()
    return acc, True

async def import_account_bundle(
    archive_path: Path,
    session_factory: Callable[[], AsyncSession],
    max_sessions: int = 20
) -> Optional[ImportBundleResult]:
    inspect_dir = Path(tempfile.mkdtemp(prefix="bundle_check_"))
    try:
        safe_extract_zip(archive_path, inspect_dir)
        session_files = list(inspect_dir.rglob("*.session"))[:max_sessions]
        if not session_files:
            return None

        proxies_file = next(inspect_dir.rglob("*proxy*.txt"), None)
        added_proxies = 0
        if proxies_file:
            async with session_factory() as session:
                added_proxies, _ = await import_proxies_from_text(
                    session, proxies_file.read_text(encoding="utf-8")
                )

        imported_count = 0
        errors_count = 0

        async with session_factory() as session:
            existing_proxies = (await session.execute(
                select(Proxy).where(Proxy.is_active == True)
            )).scalars().all()

            if settings.REQUIRE_STRICT_PROXIES and not existing_proxies:
                raise ProxySecurityError("Zero-leak policy requires an active proxy for bundle import")

            for idx, s_path in enumerate(session_files):
                chosen_proxy = None
                proxy_dict = None
                if existing_proxies:
                    chosen_proxy = existing_proxies[idx % len(existing_proxies)]
                    proxy_dict = build_proxy_dict(chosen_proxy)

                ok, _, s_info = await import_session_file(s_path, proxy=proxy_dict)
                if ok and s_info:
                    proxy_id = chosen_proxy.id if chosen_proxy else None
                    await register_single_account(session, s_info, proxy_id=proxy_id)
                    imported_count += 1
                else:
                    errors_count += 1

        return ImportBundleResult(
            imported_count=imported_count,
            errors_count=errors_count,
            added_proxies=added_proxies,
            is_session_bundle=True
        )
    finally:
        shutil.rmtree(inspect_dir, ignore_errors=True)
