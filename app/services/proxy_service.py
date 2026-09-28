import asyncio
import re
import socket
from datetime import datetime, timezone
from typing import List, Tuple, Optional
from urllib.parse import urlparse, unquote
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Proxy
from app.core.security import encrypt_session_string

ALLOWED_PROXY_PROTOCOLS = ("socks5", "socks4", "http")
HOST_PATTERN = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

def _valid_host(host: Optional[str]) -> bool:
    if not host:
        return False
    try:
        socket.inet_pton(socket.AF_INET, host)
        return True
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host)
        return True
    except OSError:
        pass
    return bool(HOST_PATTERN.match(host))

def parse_proxy_line(raw_line: str) -> Optional[Tuple[str, int, Optional[str], Optional[str], str]]:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None

    protocol = "socks5"
    if "://" in line:
        try:
            parsed = urlparse(line)
            protocol = parsed.scheme.lower() or "socks5"
            host = parsed.hostname
            port = parsed.port
            username = unquote(parsed.username) if parsed.username else None
            password = unquote(parsed.password) if parsed.password else None
        except ValueError:
            return None
        if protocol not in ALLOWED_PROXY_PROTOCOLS:
            return None
        if not _valid_host(host) or not port or not 1 <= port <= 65535:
            return None
        return host, port, username, password, protocol

    parts = line.split(":")
    try:
        if len(parts) == 2:
            port = int(parts[1])
            if 1 <= port <= 65535 and _valid_host(parts[0]):
                return parts[0], port, None, None, protocol
        elif len(parts) == 4:
            port = int(parts[1])
            if 1 <= port <= 65535 and _valid_host(parts[0]):
                return parts[0], port, parts[2], parts[3], protocol
    except ValueError:
        return None

    return None

async def check_proxy_reachability(
    host: str,
    port: int,
    protocol: str = "socks5",
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_seconds: float = 8.0
) -> Tuple[bool, Optional[str]]:
    loop = asyncio.get_running_loop()

    def _test() -> Tuple[bool, Optional[str]]:
        import socks
        s = socks.socksocket()
        proto = (protocol or "socks5").lower()
        ptype = socks.SOCKS5 if proto == "socks5" else (socks.SOCKS4 if proto == "socks4" else socks.HTTP)
        s.set_proxy(ptype, host, port, username=username, password=password)
        s.settimeout(timeout_seconds)
        try:
            s.connect(("149.154.175.54", 443))
            s.close()
            return True, None
        except Exception as exc:
            return False, str(exc)

    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _test),
            timeout=timeout_seconds + 2.0
        )
    except Exception as exc:
        return False, str(exc)

async def auto_assign_proxies(session: AsyncSession) -> int:
    from app.models.models import Account
    active_proxies = (await session.execute(
        select(Proxy).where(Proxy.is_active == True).order_by(Proxy.id.asc())
    )).scalars().all()

    if not active_proxies:
        return 0

    active_proxy_ids = [p.id for p in active_proxies]
    accounts_without_proxy = (await session.execute(
        select(Account).where(
            Account.is_active == True,
            (Account.proxy_id.is_(None)) | (Account.proxy_id.not_in(active_proxy_ids))
        ).order_by(Account.id.asc())
    )).scalars().all()

    if not accounts_without_proxy:
        return 0

    assigned_count = 0
    for idx, acc in enumerate(accounts_without_proxy):
        chosen_proxy = active_proxies[idx % len(active_proxies)]
        acc.proxy_id = chosen_proxy.id
        assigned_count += 1

    await session.commit()
    return assigned_count

async def import_proxies_from_text(session: AsyncSession, raw_text: str) -> Tuple[int, int]:
    added_count = 0
    skipped_count = 0

    lines = raw_text.strip().splitlines()
    for raw_line in lines:
        parsed = parse_proxy_line(raw_line)
        if not parsed:
            skipped_count += 1
            continue

        host, port, username, password, protocol = parsed
        existing = await session.execute(
            select(Proxy).where(
                Proxy.host == host,
                Proxy.port == port,
                Proxy.protocol == protocol,
                Proxy.username == username
            )
        )
        if existing.scalars().first():
            skipped_count += 1
            continue

        encrypted_pwd = encrypt_session_string(password) if password else None
        proxy = Proxy(
            host=host,
            port=port,
            username=username,
            password=encrypted_pwd,
            protocol=protocol,
            is_active=True
        )
        session.add(proxy)
        added_count += 1

    await session.commit()
    return added_count, skipped_count
