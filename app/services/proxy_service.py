import asyncio
import re
import socket
from datetime import datetime, timezone
from typing import List, Tuple, Optional
from urllib.parse import urlparse, unquote
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Proxy
from app.core.security import encrypt_session_string, decrypt_session_string

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

    parts = line.rsplit(":", 3)
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

    if line.startswith("[") and "]:" in line:
        try:
            host_end = line.index("]:")
            host = line[1:host_end]
            rest = line[host_end + 2:].split(":")
            if len(rest) in (1, 3) and _valid_host(host):
                port = int(rest[0])
                if 1 <= port <= 65535:
                    if len(rest) == 1:
                        return host, port, None, None, protocol
                    return host, port, rest[1], rest[2], protocol
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
        try:
            proto = (protocol or "socks5").lower()
            ptype = socks.SOCKS5 if proto == "socks5" else (socks.SOCKS4 if proto == "socks4" else socks.HTTP)
            s.set_proxy(ptype, host, port, username=username, password=password)
            s.settimeout(timeout_seconds)
            try:
                s.connect(("149.154.175.54", 443))
                return True, None
            except Exception as exc:
                return False, str(exc)
        finally:
            try:
                s.close()
            except Exception:
                pass

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

    parsed_lines = []
    for raw_line in raw_text.strip().splitlines():
        parsed = parse_proxy_line(raw_line)
        if not parsed:
            skipped_count += 1
            continue
        parsed_lines.append(parsed)

    if not parsed_lines:
        return added_count, skipped_count

    pairs = [(host, port) for host, port, _, _, _ in parsed_lines]
    pair_filter = or_(*[and_(Proxy.host == h, Proxy.port == p) for h, p in pairs])
    existing_rows = (await session.execute(
        select(Proxy).where(pair_filter)
    )).scalars().all()
    existing_by_endpoint = {(p.host, p.port, p.protocol, p.username): p for p in existing_rows}

    for host, port, username, password, protocol in parsed_lines:
        key = (host, port, protocol, username)
        known = existing_by_endpoint.get(key)
        if known:
            stored_password = None
            if known.password:
                try:
                    stored_password = decrypt_session_string(known.password)
                except ValueError:
                    stored_password = None
            password_changed = (password or None) != stored_password
            if password_changed or known.is_active is False:
                known.password = encrypt_session_string(password) if password else None
                known.is_active = True
                added_count += 1
            else:
                skipped_count += 1
            continue

        session.add(Proxy(
            host=host,
            port=port,
            username=username,
            password=encrypt_session_string(password) if password else None,
            protocol=protocol,
            is_active=True
        ))
        existing_by_endpoint[key] = True
        added_count += 1

    await session.commit()
    return added_count, skipped_count
