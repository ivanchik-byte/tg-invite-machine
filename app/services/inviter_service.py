import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional, Callable, Awaitable, List, Any
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    UserPrivacyRestrictedError,
    UserAlreadyParticipantError,
    UserNotMutualContactError,
    UserIdInvalidError,
    ChatAdminRequiredError,
    InviteRequestSentError,
    UserDeactivatedError,
    UserDeactivatedBanError,
    AuthKeyUnregisteredError,
    SessionRevokedError,
)
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.types import InputUser, InputPeerUser, InputPeerChannel, Channel
from sqlalchemy import select, and_, update, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import Account, AudienceMember, TargetGroup, InviteTask
from app.telegram.client_factory import get_telethon_client

logger = logging.getLogger("tg_invite_machine")

TaskUpdateCallback = Callable[..., Awaitable[None]]

def calculate_delay(speed_profile: str) -> float:
    base_min = settings.MIN_DELAY_BETWEEN_INVITES
    base_max = settings.MAX_DELAY_BETWEEN_INVITES
    if speed_profile.startswith("custom:"):
        try:
            parts = speed_profile.split(":")
            min_pause = float(parts[1])
            max_pause = float(parts[2])
            if min_pause > max_pause:
                min_pause, max_pause = max_pause, min_pause
        except (ValueError, IndexError):
            min_pause, max_pause = base_min, base_max
        if max_pause <= 0.0:
            return 0.0
        if max_pause <= 1.0:
            return random.uniform(min_pause, max_pause)
    else:
        profile_bounds = {
            "cautious": (base_min * 1.5, base_max * 1.5),
            "normal": (base_min, base_max),
            "fast": (max(10, base_min * 0.5), max(20, base_max * 0.5))
        }
        min_pause, max_pause = profile_bounds.get(speed_profile, (base_min, base_max))
    roll = random.random()
    if roll < 0.25:
        return max(0.0, random.uniform(min_pause * 0.7, min_pause))
    if roll < 0.85:
        return max(0.0, random.uniform(min_pause, max_pause))
    return max(0.0, random.uniform(max_pause, max_pause * 1.5))

async def simulate_pre_invite_reading(client: TelegramClient, target_entity: Any, mark_read: bool = True) -> None:
    try:
        await client(UpdateStatusRequest(offline=False))
        message_ids = []
        async for message in client.iter_messages(target_entity, limit=random.randint(3, 7)):
            if message and message.id:
                message_ids.append(message.id)
            await asyncio.sleep(random.uniform(1.2, 3.5))

        if mark_read and message_ids:
            await client.send_read_acknowledge(target_entity, max_id=max(message_ids))
    except Exception as exc:
        # read markers are cosmetic: never fail the invite over them
        logger.debug("pre-invite reading skipped: %s", exc)

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
                .where(
                    (Account.last_invite_at < today_start) | (Account.last_invite_at.is_(None)),
                    Account.daily_invites_count > 0,
                )
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

            target_input = None
            mark_read = True
            if target_group.tg_id and target_group.access_hash:
                target_input = InputPeerChannel(target_group.tg_id, target_group.access_hash)
                mark_read = target_group.chat_type != "channel"
            else:
                # legacy target without stored ids: resolve once and backfill
                probe = (await session.execute(
                    select(Account).options(selectinload(Account.proxy)).where(
                        Account.is_active == True, Account.status == "active"
                    ).limit(1)
                )).scalars().first()
                if probe:
                    probe_client = get_telethon_client(probe, proxy=probe.proxy)
                    try:
                        await probe_client.connect()
                        resolved = await probe_client.get_entity(
                            target_group.username or target_group.tg_id)
                        if isinstance(resolved, Channel):
                            target_group.chat_type = "channel" if resolved.broadcast else "supergroup"
                        mark_read = not (isinstance(resolved, Channel) and resolved.broadcast)
                        target_group.tg_id = resolved.id
                        target_group.access_hash = resolved.access_hash
                        await session.commit()
                        target_input = InputPeerChannel(resolved.id, resolved.access_hash)
                    except Exception as exc:
                        logger.warning("cannot resolve invite target: %s", exc)
                    finally:
                        await probe_client.disconnect()

            if target_input is None:
                task.status = "failed"
                await session.commit()
                return

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
                        "Сработала защита (Circuit Breaker). Слишком много флуд-ограничений подряд. Задача приостановлена.",
                        is_final=True
                    )
                break

            async with session_factory() as session:
                today_start = datetime.combine(now.date(), datetime.min.time())
                await session.execute(
                    update(Account)
                    .where(
                        (Account.last_invite_at < today_start) | (Account.last_invite_at.is_(None)),
                        Account.daily_invites_count > 0,
                    )
                    .values(daily_invites_count=0)
                )
                await session.execute(
                    update(Account)
                    .where(Account.status == "cooldown", Account.cooldown_until <= now)
                    .values(status="active", cooldown_until=None)
                )
                await session.commit()

                task = await session.get(InviteTask, self.task_id)
                if not task:
                    break
                if task.max_invites and task.successful_invites >= task.max_invites:
                    task.status = "completed"
                    task.finished_at = now
                    await session.commit()
                    if progress_callback:
                        await progress_callback(
                            self.task_id,
                            task.successful_invites,
                            task.total_targets,
                            task.flood_errors,
                            f"Заданный лимит инвайтов ({task.max_invites}) успешно достигнут.",
                            is_final=True
                        )
                    break

                is_sqlite = "sqlite" in settings.DATABASE_URL
                worker_query = select(Account).options(selectinload(Account.proxy)).where(
                    and_(
                        Account.is_active == True,
                        Account.status == "active",
                        (Account.cooldown_until.is_(None)) | (Account.cooldown_until <= now),
                        Account.daily_invites_count < settings.MAX_INVITES_PER_SESSION_DAILY
                    )
                ).order_by(Account.last_invite_at.asc().nullsfirst()).limit(1)

                if not is_sqlite:
                    worker_query = worker_query.with_for_update(skip_locked=True)

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
                            "Все доступные аккаунты исчерпали дневной лимит или находятся в отлежке.",
                            is_final=True
                        )
                    break

                target_query = select(AudienceMember).where(
                    AudienceMember.status == "pending"
                ).order_by(func.random()).limit(1)

                if not is_sqlite:
                    target_query = target_query.with_for_update(skip_locked=True)

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
                            "Очередь пользователей завершена. Все участники обработаны.",
                            is_final=True
                        )
                    break

                account_id = active_account.id
                member_id = target_member.id
                target_access_hash = target_member.access_hash
                assigned_proxy = active_account.proxy

            client: TelegramClient = get_telethon_client(active_account, proxy=assigned_proxy)
            invite_success = False
            error_status = None
            error_reason = None

            try:
                await client.connect()
                is_turbo = speed_profile.startswith("custom:") and any(
                    speed_profile.startswith(f"custom:{p}:") for p in ("0", "1")
                )
                if not is_turbo:
                    await simulate_pre_invite_reading(client, target_input, mark_read=mark_read)

                if target_member.username:
                    user_to_add = await client.get_entity(target_member.username)
                elif target_access_hash and target_member.tg_id:
                    user_to_add = InputPeerUser(target_member.tg_id, target_access_hash)
                elif target_member.tg_id:
                    user_to_add = await client.get_entity(target_member.tg_id)
                else:
                    raise ValueError("Пользователь не найден")

                res = await client(InviteToChannelRequest(target_input, [user_to_add]))

                if isinstance(res, bool):
                    invite_success = res
                else:
                    # Telegram returns messages.InvitedUsers with missing_invitees if restricted
                    missing_uids = set()
                    if hasattr(res, "missing_invitees") and isinstance(res.missing_invitees, list) and res.missing_invitees:
                        missing_uids.update(m.user_id for m in res.missing_invitees if hasattr(m, "user_id"))

                    added_uids = set()
                    if hasattr(res, "updates") and hasattr(res.updates, "users") and isinstance(res.updates.users, list):
                        added_uids.update(u.id for u in res.updates.users if hasattr(u, "id"))
                    elif hasattr(res, "users") and isinstance(res.users, list):
                        added_uids.update(u.id for u in res.users if hasattr(u, "id"))

                    target_uid = target_member.tg_id
                    if not target_uid:
                        for attr in ("id", "user_id"):
                            val = getattr(user_to_add, attr, None)
                            if isinstance(val, int):
                                target_uid = val
                                break

                    if missing_uids or (target_uid and target_uid in missing_uids):
                        invite_success = False
                        error_status = "restricted"
                        premium_req = any(
                            getattr(m, "premium_would_allow_invite", False) or getattr(m, "premium_required_for_pm", False)
                            for m in getattr(res, "missing_invitees", [])
                        )
                        error_reason = "Приватность (требуется Premium)" if premium_req else "Приватность пользователя"
                    elif hasattr(res, "updates") and isinstance(getattr(res.updates, "users", None), list) and not added_uids:
                        # Telegram silently ignored invite (user already in group or privacy)
                        invite_success = False
                        error_status = "already_participant"
                        error_reason = "Уже состоит в группе или скрыт"
                    elif target_uid and added_uids and target_uid not in added_uids:
                        invite_success = False
                        error_status = "restricted"
                        error_reason = "Приватность пользователя"
                    else:
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
                error_status = "awaiting_approval"
                error_reason = "Отправлена заявка на вступление"
            except (UserDeactivatedError, UserDeactivatedBanError, AuthKeyUnregisteredError, SessionRevokedError) as ban_err:
                error_status = "account_banned"
                error_reason = f"Аккаунт заблокирован или сессия отозвана: {ban_err}"
                async with session_factory() as session:
                    db_acc = await session.get(Account, account_id)
                    if db_acc:
                        db_acc.status = "banned"
                        db_acc.is_active = False
                        await session.commit()
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
                        if task.max_invites and task.successful_invites >= task.max_invites:
                            task.status = "completed"
                            task.finished_at = now
                else:
                    if db_member:
                        if error_status == "account_banned":
                            # Account was banned; preserve member as pending for next worker
                            db_member.status = "pending"
                            db_member.reason = None
                        elif error_status in ("flood_wait", "peer_flood"):
                            # flood hit: park member out of pending so the next
                            # LIMIT 1 pick doesn't retry the same user
                            db_member.status = "deferred"
                            db_member.reason = error_reason
                        else:
                            db_member.status = error_status or "failed"
                            db_member.reason = error_reason
                    if task:
                        if error_status == "restricted":
                            task.restricted_count += 1
                        elif error_status in ("flood_wait", "peer_flood"):
                            task.flood_errors += 1

                try:
                    await session.commit()
                except asyncio.CancelledError:
                    await session.commit()
                    raise

                if task and task.status == "completed":
                    if progress_callback:
                        await progress_callback(
                            self.task_id,
                            task.successful_invites,
                            task.total_targets,
                            task.flood_errors,
                            f"Заданный лимит инвайтов ({task.max_invites}) успешно достигнут.",
                            is_final=True
                        )
                    break

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
                            "Целевая группа требует прав администратора для добавления участников. Задача приостановлена.",
                            is_final=True
                        )
                    break

                user_tag = f"@{target_member.username}" if target_member.username else (
                    target_member.first_name or f"id:{target_member.tg_id or member_id}"
                )
                if invite_success:
                    current_status = f"Пользователь {user_tag} добавлен"
                else:
                    current_status = f"Пользователь {user_tag}: пропуск ({error_reason})"

                if progress_callback and task:
                    await progress_callback(
                        self.task_id,
                        task.successful_invites,
                        task.total_targets,
                        task.flood_errors,
                        current_status,
                        is_final=False
                    )

            if invite_success or error_status in ("flood_wait", "peer_flood"):
                pause_time = calculate_delay(speed_profile)
            else:
                is_turbo = speed_profile.startswith("custom:") and any(
                    speed_profile.startswith(f"custom:{p}:") for p in ("0", "1")
                )
                if is_turbo:
                    pause_time = calculate_delay(speed_profile)
                else:
                    pause_time = random.uniform(2.0, 4.0)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=pause_time)
                break
            except asyncio.TimeoutError:
                pass
