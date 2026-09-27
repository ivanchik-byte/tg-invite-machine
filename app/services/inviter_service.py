import asyncio
import random
from datetime import datetime, timedelta, timezone
from typing import Optional, Callable, Awaitable, List
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    UserPrivacyRestrictedError,
    UserAlreadyParticipantError,
    UserNotMutualContactError,
    UserIdInvalidError,
    ChatAdminRequiredError,
    InviteRequestSentError
)
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.types import InputUser, InputPeerUser
from sqlalchemy import select, and_, update
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import Account, AudienceMember, TargetGroup, InviteTask
from app.telegram.client_factory import get_telethon_client

TaskUpdateCallback = Callable[[int, int, int, int, str], Awaitable[None]]

def calculate_delay(speed_profile: str) -> float:
    base_min = settings.MIN_DELAY_BETWEEN_INVITES
    base_max = settings.MAX_DELAY_BETWEEN_INVITES
    profile_bounds = {
        "cautious": (base_min * 1.5, base_max * 1.5),
        "normal": (base_min, base_max),
        "fast": (max(10, base_min * 0.5), max(20, base_max * 0.5))
    }
    min_pause, max_pause = profile_bounds.get(speed_profile, (base_min, base_max))
    roll = random.random()
    if roll < 0.25:
        return random.uniform(min_pause * 0.7, min_pause)
    if roll < 0.85:
        return random.uniform(min_pause, max_pause)
    return random.uniform(max_pause, max_pause * 1.5)

async def simulate_pre_invite_reading(client: TelegramClient, target_entity: any) -> None:
    try:
        await client(UpdateStatusRequest(offline=False))
        message_ids = []
        async for message in client.iter_messages(target_entity, limit=random.randint(3, 7)):
            if message and message.id:
                message_ids.append(message.id)
            await asyncio.sleep(random.uniform(1.2, 3.5))

        if message_ids:
            await client.send_read_acknowledge(target_entity, max_id=max(message_ids))
    except Exception:
        pass

class InviterOrchestrator:
    def __init__(self, task_id: int):
        self.task_id = task_id
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._pause_event.set()

    @property
    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    def pause(self) -> None:
        self._pause_event.clear()

    def resume(self) -> None:
        self._pause_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._pause_event.set()

    async def run(
        self,
        session_factory: Callable[[], AsyncSession],
        progress_callback: Optional[TaskUpdateCallback] = None
    ) -> None:
        today_start = datetime.combine(datetime.now(timezone.utc).date(), datetime.min.time())
        async with session_factory() as session:
            await session.execute(
                update(Account)
                .where(Account.last_invite_at < today_start, Account.daily_invites_count > 0)
                .values(daily_invites_count=0)
            )
            await session.commit()

        async with session_factory() as session:
            task = await session.get(InviteTask, self.task_id)
            if not task:
                return
            target_group = await session.get(TargetGroup, task.target_group_id)
            if not target_group:
                task.status = "failed"
                await session.commit()
                return

            task.status = "running"
            await session.commit()
            speed_profile = task.speed_profile
            target_identifier = target_group.username or target_group.tg_id

        circuit_breaker_floods = 0
        circuit_breaker_window_start = datetime.now(timezone.utc)

        while not self._stop_event.is_set():
            await self._pause_event.wait()
            if self._stop_event.is_set():
                break

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if (datetime.now(timezone.utc) - circuit_breaker_window_start).total_seconds() > 1800:
                circuit_breaker_floods = 0
                circuit_breaker_window_start = datetime.now(timezone.utc)

            if circuit_breaker_floods >= settings.CIRCUIT_BREAKER_FLOOD_THRESHOLD:
                async with session_factory() as session:
                    task = await session.get(InviteTask, self.task_id)
                    if task:
                        task.status = "paused"
                        await session.commit()
                if progress_callback:
                    await progress_callback(
                        self.task_id,
                        task.successful_invites,
                        task.total_targets,
                        task.flood_errors,
                        "Сработала защита (Circuit Breaker). Слишком много флуд-ограничений подряд. Задача приостановлена."
                    )
                break

            async with session_factory() as session:
                await session.execute(
                    update(Account)
                    .where(Account.status == "cooldown", Account.cooldown_until <= now)
                    .values(status="active", cooldown_until=None)
                )
                await session.commit()

                worker_query = select(Account).options(selectinload(Account.proxy)).where(
                    and_(
                        Account.is_active == True,
                        Account.status == "active",
                        (Account.cooldown_until == None) | (Account.cooldown_until <= now),
                        Account.daily_invites_count < settings.MAX_INVITES_PER_SESSION_DAILY
                    )
                ).order_by(Account.last_invite_at.asc().nullsfirst()).limit(1)

                active_account = (await session.execute(worker_query)).scalars().first()
                if not active_account:
                    task = await session.get(InviteTask, self.task_id)
                    if task:
                        task.status = "completed"
                        task.finished_at = now
                        await session.commit()
                    if progress_callback:
                        await progress_callback(
                            self.task_id,
                            task.successful_invites if task else 0,
                            task.total_targets if task else 0,
                            task.flood_errors if task else 0,
                            "Все доступные аккаунты исчерпали дневной лимит или находятся в отлежке."
                        )
                    break

                target_query = select(AudienceMember).where(
                    and_(
                        AudienceMember.status == "pending",
                        (AudienceMember.target_group_id == None) | (AudienceMember.target_group_id == target_group.id)
                    )
                ).order_by(AudienceMember.id.asc()).limit(1)
                target_member = (await session.execute(target_query)).scalars().first()
                if not target_member:
                    task = await session.get(InviteTask, self.task_id)
                    if task:
                        task.status = "completed"
                        task.finished_at = now
                        await session.commit()
                    if progress_callback:
                        await progress_callback(
                            self.task_id,
                            task.successful_invites if task else 0,
                            task.total_targets if task else 0,
                            task.flood_errors if task else 0,
                            "Очередь пользователей завершена. Все участники обработаны."
                        )
                    break

                account_id = active_account.id
                member_id = target_member.id
                target_user_ref = target_member.username or target_member.tg_id
                target_access_hash = target_member.access_hash
                assigned_proxy = active_account.proxy

            client: TelegramClient = get_telethon_client(active_account, proxy=assigned_proxy)
            invite_success = False
            error_status = None
            error_reason = None

            try:
                await client.connect()
                target_chat_entity = await client.get_entity(target_identifier)
                await simulate_pre_invite_reading(client, target_chat_entity)

                if target_access_hash and target_member.tg_id:
                    user_to_add = InputPeerUser(target_member.tg_id, target_access_hash)
                elif target_member.username:
                    user_to_add = await client.get_entity(target_member.username)
                elif target_member.tg_id:
                    user_to_add = await client.get_entity(target_member.tg_id)
                else:
                    raise ValueError("Пользователь не найден")

                await client(InviteToChannelRequest(target_chat_entity, [user_to_add]))
                invite_success = True

            except UserPrivacyRestrictedError:
                error_status = "restricted"
                error_reason = "Приватность пользователя"
            except UserAlreadyParticipantError:
                error_status = "already_participant"
                error_reason = "Уже состоит в группе"
            except UserNotMutualContactError:
                error_status = "restricted"
                error_reason = "Требуется взаимный контакт"
            except (UserIdInvalidError, ValueError):
                error_status = "skipped"
                error_reason = "Пользователь не найден"
            except InviteRequestSentError:
                error_status = "pending"
                error_reason = "Отправлена заявка на вступление"
            except FloodWaitError as flood:
                error_status = "flood_wait"
                error_reason = f"FloodWait {flood.seconds}s"
                circuit_breaker_floods += 1
                async with session_factory() as session:
                    db_acc = await session.get(Account, account_id)
                    if db_acc:
                        db_acc.cooldown_until = now + timedelta(seconds=flood.seconds + 60)
                        db_acc.flood_incidents += 1
                        await session.commit()
            except PeerFloodError:
                error_status = "peer_flood"
                error_reason = "Инвайт-бан (PeerFlood)"
                circuit_breaker_floods += 1
                async with session_factory() as session:
                    db_acc = await session.get(Account, account_id)
                    if db_acc:
                        db_acc.status = "cooldown"
                        db_acc.cooldown_until = now + timedelta(hours=24)
                        db_acc.flood_incidents += 1
                        await session.commit()
            except ChatAdminRequiredError:
                error_status = "no_rights"
                error_reason = "У аккаунта нет прав на добавление участников"
            except Exception as unhandled_error:
                error_status = "failed"
                error_reason = str(unhandled_error)
            finally:
                await client.disconnect()

            async with session_factory() as session:
                task = await session.get(InviteTask, self.task_id)
                db_member = await session.get(AudienceMember, member_id)
                db_account = await session.get(Account, account_id)

                if invite_success:
                    if db_member:
                        db_member.status = "invited"
                        db_member.target_group_id = target_group.id
                        db_member.invited_by_account_id = account_id
                    if db_account:
                        db_account.record_invite(now)
                    if task:
                        task.successful_invites += 1
                else:
                    if db_member:
                        if error_status in ("flood_wait", "peer_flood"):
                            pass
                        else:
                            db_member.status = error_status or "failed"
                            db_member.reason = error_reason
                    if task:
                        if error_status == "restricted":
                            task.restricted_count += 1
                        elif error_status in ("flood_wait", "peer_flood"):
                            task.flood_errors += 1

                await session.commit()

                if error_status == "no_rights":
                    if task:
                        task.status = "paused"
                        await session.commit()
                    if progress_callback:
                        await progress_callback(
                            self.task_id,
                            task.successful_invites if task else 0,
                            task.total_targets if task else 0,
                            task.flood_errors if task else 0,
                            "Целевая группа требует прав администратора для добавления участников. Задача приостановлена."
                        )
                    break

                if progress_callback and task:
                    progress_info = (
                        f"В процессе [{task.speed_profile}]: добавлено {task.successful_invites} | "
                        f"приватных {task.restricted_count} | флуд {task.flood_errors}"
                    )
                    await progress_callback(
                        self.task_id,
                        task.successful_invites,
                        task.total_targets,
                        task.flood_errors,
                        progress_info
                    )

            pause_time = calculate_delay(speed_profile)
            await asyncio.sleep(pause_time)
