import asyncio
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Set, Optional, Callable, Awaitable
from telethon import TelegramClient
from telethon.tl.types import (
    Channel,
    User,
    ChannelParticipantsSearch,
)
from telethon.tl.functions.channels import GetFullChannelRequest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DATA_DIR
from app.models.models import Account, AudienceMember
from app.telegram.client_factory import get_telethon_client

ProgressCallback = Callable[[int, str], Awaitable[None]]

async def collect_chat_members(
    session: AsyncSession,
    account: Account,
    chat_identifier: str,
    active_days: Optional[int] = None,
    progress_callback: Optional[ProgressCallback] = None
) -> tuple[int, int, Path]:
    client: TelegramClient = get_telethon_client(account)
    await client.connect()

    try:
        chat_entity = await client.get_entity(chat_identifier)
        if isinstance(chat_entity, Channel) and chat_entity.broadcast:
            full_channel = await client(GetFullChannelRequest(chat_entity))
            if full_channel.full_chat.linked_chat_id:
                chat_entity = await client.get_entity(full_channel.full_chat.linked_chat_id)
            else:
                raise ValueError("Указанный канал не имеет открытой группы обсуждений для сбора участников")

        collected_users: dict[int, User] = {}
        cutoff_date = None
        if active_days:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=active_days)

        if active_days:
            if progress_callback:
                await progress_callback(0, "Сканирование авторов активных сообщений...")

            async for message in client.iter_messages(chat_entity, limit=3000):
                if not message.sender or not isinstance(message.sender, User):
                    continue
                if message.date and cutoff_date and message.date < cutoff_date:
                    break

                user: User = message.sender
                if user.bot or user.deleted:
                    continue

                collected_users[user.id] = user
                if progress_callback and len(collected_users) % 50 == 0:
                    await progress_callback(len(collected_users), f"Собрано {len(collected_users)} активных пользователей...")
        else:
            if progress_callback:
                await progress_callback(0, "Сбор списка участников чата...")

            alphabet = [""] + list(string.ascii_lowercase) + [str(d) for d in range(10)]
            seen_ids: Set[int] = set()

            for query_char in alphabet:
                search_filter = ChannelParticipantsSearch(query_char) if query_char else None
                async for participant in client.iter_participants(chat_entity, filter=search_filter, limit=5000):
                    if not isinstance(participant, User) or participant.bot or participant.deleted:
                        continue
                    if participant.id in seen_ids:
                        continue

                    seen_ids.add(participant.id)
                    collected_users[participant.id] = participant

                if progress_callback:
                    await progress_callback(len(collected_users), f"Собрано {len(collected_users)} участников...")

                if len(alphabet) > 1 and len(collected_users) > 10000:
                    break

        new_saved = 0
        existing_skipped = 0
        source_name = getattr(chat_entity, "username", None) or getattr(chat_entity, "title", str(chat_identifier))

        all_users = list(collected_users.values())
        user_ids = [u.id for u in all_users]

        existing_ids = set()
        for i in range(0, len(user_ids), 500):
            chunk = user_ids[i:i + 500]
            query_res = await session.execute(
                select(AudienceMember.tg_id).where(
                    AudienceMember.tg_id.in_(chunk),
                    AudienceMember.source_chat == str(source_name)
                )
            )
            existing_ids.update(query_res.scalars().all())

        new_members = []
        for tg_user in all_users:
            if tg_user.id in existing_ids:
                existing_skipped += 1
                continue

            member = AudienceMember(
                tg_id=tg_user.id,
                access_hash=getattr(tg_user, "access_hash", None),
                username=tg_user.username,
                first_name=tg_user.first_name,
                last_name=tg_user.last_name,
                source_chat=str(source_name),
                status="pending"
            )
            new_members.append(member)
            new_saved += 1

        if new_members:
            try:
                session.add_all(new_members)
                await session.commit()
            except IntegrityError:
                await session.rollback()
                for member in new_members:
                    try:
                        session.add(member)
                        await session.commit()
                    except IntegrityError:
                        await session.rollback()
                        new_saved -= 1
                        existing_skipped += 1

        export_dir = DATA_DIR / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        export_filename = export_dir / f"export_{int(datetime.now().timestamp())}.txt"

        lines = []
        for tg_user in collected_users.values():
            if tg_user.username:
                lines.append(f"@{tg_user.username}")
            else:
                lines.append(str(tg_user.id))

        await asyncio.to_thread(export_filename.write_text, "\n".join(lines), encoding="utf-8")

        return new_saved, existing_skipped, export_filename

    finally:
        await client.disconnect()
