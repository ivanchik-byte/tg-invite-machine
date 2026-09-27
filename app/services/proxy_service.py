import asyncio
import socket
from datetime import datetime, timezone
from typing import List, Tuple, Optional
from urllib.parse import urlparse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Proxy

def parse_proxy_line(raw_line: str) -> Optional[Tuple[str, int, Optional[str], Optional[str], str]]:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None

    protocol = "socks5"
    if "://" in line:
        parsed = urlparse(line)
        protocol = parsed.scheme.lower() or "socks5"
        host = parsed.hostname
        port = parsed.port
        username = parsed.username
        password = parsed.password
        if host and port:
            return host, port, username, password, protocol
        return None

    parts = line.split(":")
    try:
        if len(parts) == 2:
            port = int(parts[1])
            if 1 <= port <= 65535:
                return parts[0], port, None, None, protocol
        elif len(parts) == 4:
            port = int(parts[1])
            if 1 <= port <= 65535:
                return parts[0], port, parts[2], parts[3], protocol
    except ValueError:
        return None

    return None

async def check_proxy_reachability(host: str, port: int, timeout_seconds: float = 5.0) -> Tuple[bool, Optional[str]]:
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: socket.create_connection((host, port), timeout=timeout_seconds).close()),
            timeout=timeout_seconds + 1.0
        )
        return True, None
    except Exception as exc:
        return False, str(exc)

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
                Proxy.username == username
            )
        )
        if existing.scalars().first():
            skipped_count += 1
            continue

        proxy = Proxy(
            host=host,
            port=port,
            username=username,
            password=password,
            protocol=protocol,
            is_active=True
        )
        session.add(proxy)
        added_count += 1

    await session.commit()
    return added_count, skipped_count
