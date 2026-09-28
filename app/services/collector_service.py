import asyncio
import logging
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Set, Optional, Callable, Awaitable
from telethon import TelegramClient, errors
from telethon.tl.types import (
    Channel,
    User,
    ChannelParticipantsSearch,
    UserStatusOnline,
    UserStatusRecently,
)
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from sqlalchemy import select, func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DATA_DIR
from app.models.models import Account, AudienceMember, AudienceHistory, utc_now
from app.telegram.client_factory import get_telethon_client

logger = logging.getLogger("tg_invite_machine")

ProgressCallback = Callable[[int, str, Optional[str]], Awaitable[None]]

# online now or active within last 1-3 days; excludes dormant accounts early
FRESH_STATUSES = (UserStatusOnline, UserStatusRecently)


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
        try:
            chat_entity = await client.get_entity(chat_identifier)
        except Exception:
            if "joinchat/" in str(chat_identifier) or "+" in str(chat_identifier):
                invite_hash = str(chat_identifier).split("/")[-1].replace("+", "")
                updates = await client(ImportChatInviteRequest(invite_hash))
                chat_entity = updates.chats[0]
            else:
                raise

        if isinstance(chat_entity, Channel) and chat_entity.broadcast:
            full_channel = await client(GetFullChannelRequest(chat_entity))
            if full_channel.full_chat.linked_chat_id:
                chat_entity = await client.get_entity(full_channel.full_chat.linked_chat_id)
            else:
                raise ValueError("Указанный канал не имеет открытой группы обсуждений для сбора участников")

        # join if not already a member so iter_participants works
        if isinstance(chat_entity, Channel) and not getattr(chat_entity, "left", False) and getattr(chat_entity, "participant", None) is None:
            try:
                await client(JoinChannelRequest(chat_entity))
            except Exception as exc:
                logger.debug("auto-join skipped: %s", exc)

        collected_users: dict[int, User] = {}
        cutoff_date = None
        if active_days:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=active_days)

        if active_days:
            if progress_callback:
                await progress_callback(0, "Сканирование авторов активных сообщений...", None)

            async for message in client.iter_messages(chat_entity, limit=3000):
                if not message.sender or not isinstance(message.sender, User):
                    continue
                if message.date and cutoff_date and message.date < cutoff_date:
                    break

                user: User = message.sender
                if user.bot or user.deleted:
                    continue

                if user.id not in collected_users:
                    collected_users[user.id] = user
                    if progress_callback:
                        u_tag = f"@{user.username}" if user.username else f"ID:{user.id}"
                        if user.first_name:
                            u_tag += f" ({user.first_name[:18]})"
                        await progress_callback(len(collected_users), f"Собрано {len(collected_users)} активных пользователей...", u_tag)
        else:
            if progress_callback:
                await progress_callback(0, "Сбор списка участников чата...", None)

            cyrillic = [chr(c) for c in range(ord('а'), ord('я') + 1)]
            alphabet = [""] + list(string.ascii_lowercase) + cyrillic + [str(d) for d in range(10)]
            seen_ids: Set[int] = set()

            try:
                for query_char in alphabet:
                    try:
                        search_filter = ChannelParticipantsSearch(query_char) if query_char else None
                        async for participant in client.iter_participants(chat_entity, filter=search_filter, limit=5000):
                            if not isinstance(participant, User) or participant.bot or participant.deleted:
                                continue
                            if participant.id in seen_ids:
                                continue

                            seen_ids.add(participant.id)
                            collected_users[participant.id] = participant

                            if progress_callback:
                                u_tag = f"@{participant.username}" if participant.username else f"ID:{participant.id}"
                                if participant.first_name:
                                    u_tag += f" ({participant.first_name[:18]})"
                                await progress_callback(len(collected_users), f"Собрано {len(collected_users)} участников...", u_tag)

                        if len(collected_users) > 10000:
                            break
                    except errors.FloodWaitError as flood:
                        if progress_callback:
                            await progress_callback(
                                len(collected_users),
                                f"Сбор прерван лимитом Telegram, подождите {flood.seconds} сек...",
                                None,
                            )
                        raise
            except errors.ChatAdminRequiredError:
                if not collected_users:
                    raise RuntimeError(
                        "Список участников этой супергруппы скрыт администрацией. "
                        "Используйте режим «Собрать активных по дням» для сбора авторов сообщений."
                    )

        new_saved = 0
        existing_skipped = 0
        source_name = getattr(chat_entity, "username", None) or getattr(chat_entity, "title", str(chat_identifier))

        all_users = list(collected_users.values())
        user_ids = [u.id for u in all_users]

        existing_ids = set()
        existing_usernames = set()

        for i in range(0, len(user_ids), 500):
            chunk_ids = user_ids[i:i + 500]
            known_members = await session.execute(
                select(AudienceMember.tg_id).where(AudienceMember.tg_id.in_(chunk_ids))
            )
            existing_ids.update(known_members.scalars().all())

            known_history = await session.execute(
                select(AudienceHistory.tg_id).where(AudienceHistory.tg_id.in_(chunk_ids))
            )
            existing_ids.update(known_history.scalars().all())

        raw_usernames = [u.username.lower() for u in all_users if u.username]
        for i in range(0, len(raw_usernames), 500):
            chunk_un = raw_usernames[i:i + 500]
            known_member_names = await session.execute(
                select(func.lower(AudienceMember.username)).where(
                    func.lower(AudienceMember.username).in_(chunk_un)
                )
            )
            existing_usernames.update(known_member_names.scalars().all())

            known_history_names = await session.execute(
                select(func.lower(AudienceHistory.username)).where(
                    func.lower(AudienceHistory.username).in_(chunk_un)
                )
            )
            existing_usernames.update(known_history_names.scalars().all())

        # reactivate deferred members if they re-appeared in donor chat
        for i in range(0, len(user_ids), 500):
            chunk_ids = user_ids[i:i + 500]
            await session.execute(
                update(AudienceMember)
                .where(
                    AudienceMember.tg_id.in_(chunk_ids),
                    AudienceMember.status == "deferred"
                )
                .values(status="pending", reason=None)
            )

        new_members = []
        new_history = []
        for tg_user in all_users:
            un_lower = tg_user.username.lower() if tg_user.username else None
            if tg_user.id in existing_ids or (un_lower and un_lower in existing_usernames):
                existing_skipped += 1
                continue

            # snapshot status at collect time so inviter queue filters without extra RPC
            seen_at = utc_now() if isinstance(getattr(tg_user, "status", None), FRESH_STATUSES) else None
            member = AudienceMember(
                tg_id=tg_user.id,
                access_hash=getattr(tg_user, "access_hash", None),
                username=tg_user.username,
                first_name=tg_user.first_name,
                last_name=tg_user.last_name,
                source_chat=str(source_name),
                last_seen_at=seen_at,
                status="pending"
            )
            history_record = AudienceHistory(
                tg_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                last_name=tg_user.last_name,
                source_chat=str(source_name),
                status="collected"
            )
            new_members.append(member)
            new_history.append(history_record)
            existing_ids.add(tg_user.id)
            if un_lower:
                existing_usernames.add(un_lower)
            new_saved += 1

        if new_members:
            try:
                session.add_all(new_members)
                session.add_all(new_history)
                await session.commit()
            except IntegrityError:
                await session.rollback()
                for member, hist in zip(new_members, new_history):
                    try:
                        session.add(member)
                        session.add(hist)
                        await session.commit()
                    except IntegrityError:
                        await session.rollback()
                        new_saved -= 1
                        existing_skipped += 1

        export_dir = DATA_DIR / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        export_filename = export_dir / f"export_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.txt"

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
