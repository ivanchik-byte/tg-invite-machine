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
    UserChannelsTooMuchError,
    UserIdInvalidError,
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    ChannelInvalidError,
    ChannelPrivateError,
    InviteRequestSentError,
    UserDeactivatedError,
    UserDeactivatedBanError,
    AuthKeyUnregisteredError,
    AuthKeyDuplicatedError,
    SessionRevokedError,
)
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.functions.channels import InviteToChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import AddChatUserRequest
from telethon.tl.types import InputUser, InputPeerUser, InputPeerChannel, InputPeerChat, Channel, Chat
from sqlalchemy import select, and_, or_, update, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.settings_service import get_daily_invite_limit, get_privacy_blacklist_enabled, get_recent_only_enabled, get_excluded_worker_ids
from app.models.models import Account, AudienceMember, TargetGroup, InviteTask, AudienceHistory
from app.telegram.client_factory import get_telethon_client
from app.services.account_service import auto_recover_cooldowns


logger = logging.getLogger("tg_invite_machine")

TaskUpdateCallback = Callable[..., Awaitable[None]]

def calculate_delay(speed_profile: str) -> float:
    base_min = float(settings.MIN_DELAY_BETWEEN_INVITES)
    base_max = float(settings.MAX_DELAY_BETWEEN_INVITES)
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
        # custom interval bypasses trimodal jitter to respect user range exactly
        return random.uniform(min_pause, max_pause)
    else:
        profile_bounds = {
            "cautious": (base_min * 1.5, base_max * 1.5),
            "normal": (base_min, base_max),
            "fast": (max(10.0, base_min * 0.5), max(20.0, base_max * 0.5))
        }
        min_pause, max_pause = profile_bounds.get(speed_profile, (base_min, base_max))
        roll = random.random()
        if roll < 0.25:
            return max(0.0, random.uniform(min_pause * 0.85, min_pause))
        if roll < 0.85:
            return max(0.0, random.uniform(min_pause, max_pause))
        return max(0.0, random.uniform(max_pause, max_pause * 1.2))

async def simulate_pre_invite_reading(client: TelegramClient, target_entity: Any, mark_read: bool = True) -> None:
    try:
        await client(UpdateStatusRequest(offline=False))
        message_ids = []
        async for message in client.iter_messages(target_entity, limit=random.randint(2, 4)):
            if message and message.id:
                message_ids.append(message.id)
            await asyncio.sleep(random.uniform(0.5, 1.2))

        if mark_read and message_ids:
            await client.send_read_acknowledge(target_entity, max_id=max(message_ids))
    except Exception as exc:
        logger.debug("pre-invite reading skipped: %s", exc)

class InviterOrchestrator:
    def __init__(self, task_id: int, concurrency_mode: Optional[str] = "sequential"):
        self.task_id = task_id
        self.concurrency_mode = concurrency_mode or "sequential"
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self.recent_events: List[str] = []
        self.worker_stats: dict[str, int] = {}
        self.circuit_breaker_floods = 0
        self.circuit_breaker_window_start = datetime.now(timezone.utc).replace(tzinfo=None)
        self.task_excluded_workers: set[int] = set()
        self.worker_target_cache: dict[int, Any] = {}
        self._state_lock = asyncio.Lock()
        self._claim_lock = asyncio.Lock()
        self._db_lock = asyncio.Lock()

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

    def _check_circuit_breaker(self) -> bool:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if (now - self.circuit_breaker_window_start).total_seconds() > 1800:
            self.circuit_breaker_floods = 0
            self.circuit_breaker_window_start = now
        return self.circuit_breaker_floods >= settings.CIRCUIT_BREAKER_FLOOD_THRESHOLD

    async def _trip_circuit_breaker(
        self,
        session_factory: Callable[[], AsyncSession],
        progress_callback: Optional[TaskUpdateCallback]
    ) -> None:
        async with session_factory() as session:
            task = await session.get(InviteTask, self.task_id)
            if task:
                task.status = "paused"
                await session.commit()
        if progress_callback:
            await progress_callback(
                self.task_id,
                task.successful_invites if task else 0,
                task.total_targets if task else 0,
                task.flood_errors if task else 0,
                "Сработала защита (Circuit Breaker). Слишком много флуд-ограничений подряд. Задача приостановлена.",
                is_final=True
            )

    def _format_status_text(self, current_line: str) -> str:
        history_block = "\n".join(self.recent_events) if self.recent_events else "• Нет событий"
        worker_stats_line = (
            f"\n• <b>Воркеры:</b> <code>{' | '.join(f'{k}: {v}' for k, v in sorted(self.worker_stats.items()))}</code>\n"
            if self.worker_stats else ""
        )
        return (
            f"{current_line}\n"
            f"{worker_stats_line}\n"
            f"<b>История последних попыток:</b>\n{history_block}"
        )

    def _build_completion_message(self, max_invites: Optional[int]) -> str:
        stats_summary = ""
        if self.worker_stats:
            stats_summary = "\nВоркеры: " + " | ".join(f"{k}: {v}" for k, v in sorted(self.worker_stats.items()))
        return f"Заданный лимит инвайтов ({max_invites}) успешно достигнут.{stats_summary}"

    def _build_queue_finished_message(self) -> str:
        stats_summary = ""
        if self.worker_stats:
            stats_summary = "\nВоркеры: " + " | ".join(f"{k}: {v}" for k, v in sorted(self.worker_stats.items()))
        return f"Очередь пользователей завершена. Все участники обработаны.{stats_summary}"

    async def _record_attempt(
        self,
        invite_success: bool,
        error_status: Optional[str],
        error_reason: Optional[str],
        account_label: str,
        user_tag: str,
    ) -> tuple[str, str]:
        async with self._state_lock:
            if invite_success:
                self.circuit_breaker_floods = 0
                self.worker_stats[account_label] = self.worker_stats.get(account_label, 0) + 1
                event_str = f"• <code>{user_tag}</code> - добавлен ({account_label})"
                current_line = f"Пользователь {user_tag} добавлен через {account_label}"
            else:
                if error_status in ("flood_wait", "peer_flood", "auth_key_duplicated"):
                    self.circuit_breaker_floods += 1
                event_str = f"• <code>{user_tag}</code> - пропуск ({error_reason}) [{account_label}]"
                current_line = f"Пользователь {user_tag}: пропуск ({error_reason}) [{account_label}]"

            self.recent_events.insert(0, event_str)
            self.recent_events = self.recent_events[:5]

            current_status = self._format_status_text(current_line)
            return current_line, current_status

    async def _handle_no_workers(
        self,
        session_factory: Callable[[], AsyncSession],
        progress_callback: Optional[TaskUpdateCallback],
        now: datetime,
    ) -> bool:
        async with session_factory() as session:
            daily_limit = await get_daily_invite_limit(session=session)
            total_active = (await session.execute(
                select(func.count(Account.id)).where(Account.is_active == True, Account.status == "active")
            )).scalar_one()
            cooldown_accounts = (await session.execute(
                select(func.count(Account.id)).where(Account.is_active == True, Account.status == "cooldown")
            )).scalar_one()
            task = await session.get(InviteTask, self.task_id)

            if cooldown_accounts > 0 and total_active == 0:
                min_cd = (await session.execute(
                    select(func.min(Account.cooldown_until)).where(
                        Account.is_active == True,
                        Account.status == "cooldown",
                        Account.cooldown_until.is_not(None)
                    )
                )).scalar_one()

                wait_sec = (min_cd - now).total_seconds() if min_cd else 0
                if wait_sec <= 0:
                    await auto_recover_cooldowns(session)
                    await session.commit()
                    return True

                max_auto_wait = max(600, settings.PEER_FLOOD_COOLDOWN_MINUTES * 60 + 30)
                if wait_sec <= max_auto_wait:
                    if progress_callback and task:
                        await progress_callback(
                            self.task_id,
                            task.successful_invites,
                            task.total_targets,
                            task.flood_errors,
                            f"Все сессии в отлежке. Ожидание окончания флуд-паузы ({int(wait_sec)} сек)... Автоматическое возобновление.",
                            is_final=False
                        )
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=wait_sec + 2)
                        return False
                    except asyncio.TimeoutError:
                        return True

                if task:
                    task.status = "paused"
                    await session.commit()
                cd_time_str = min_cd.strftime("%H:%M:%S") if min_cd else "позже"
                msg = f"Все сессии в отлежке (до {cd_time_str}). Задача на паузе."
                if progress_callback:
                    await progress_callback(
                        self.task_id,
                        task.successful_invites if task else 0,
                        task.total_targets if task else 0,
                        task.flood_errors if task else 0,
                        msg,
                        is_final=True
                    )
                return False
            else:
                if task:
                    task.status = "completed"
                    task.finished_at = now
                    await session.commit()
                if progress_callback:
                    if self.task_excluded_workers and len(self.task_excluded_workers) >= total_active:
                        msg = "Сессии не состоят в целевом чате или не имеют прав для инвайтинга."
                    elif total_active > 0:
                        msg = f"Все активные сессии достигли дневного лимита ({daily_limit} инвайтов)."
                    else:
                        msg = "В пуле нет активных аккаунтов для инвайтинга."
                    await progress_callback(
                        self.task_id,
                        task.successful_invites if task else 0,
                        task.total_targets if task else 0,
                        task.flood_errors if task else 0,
                        msg,
                        is_final=True
                    )
                return False

    async def _execute_single_invite(
        self,
        session_factory: Callable[[], AsyncSession],
        account_id: int,
        member_id: int,
        target_group_data: dict,
        target_input: Any,
        mark_read: bool,
        speed_profile: str,
    ) -> dict:
        async with session_factory() as session:
            active_account = (await session.execute(
                select(Account).options(selectinload(Account.proxy)).where(Account.id == account_id)
            )).scalars().first()
            target_member = await session.get(AudienceMember, member_id)
            if not active_account or not target_member:
                return {
                    "success": False,
                    "error_status": "skipped",
                    "error_reason": "Аккаунт или пользователь не найден",
                    "account_label": f"акк #{account_id}",
                    "user_tag": f"id:{member_id}",
                    "current_line": "Пользователь не найден",
                    "current_status": self._format_status_text("Пользователь не найден")
                }

            account_label = f"+{active_account.phone.lstrip('+')}" if active_account.phone else f"акк #{account_id}"
            assigned_proxy = active_account.proxy
            target_access_hash = target_member.access_hash
            target_tg_id = target_member.tg_id
            target_username = target_member.username
            target_first_name = target_member.first_name
            target_group_id = target_group_data["id"]
            target_group_username = target_group_data.get("username")
            target_group_tg_id = target_group_data.get("tg_id")
            user_tag = f"@{target_username}" if target_username else (
                target_first_name or f"id:{target_tg_id or member_id}"
            )

        client: TelegramClient = get_telethon_client(active_account, proxy=assigned_proxy)
        invite_success = False
        error_status = None
        error_reason = None
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        try:
            await client.connect()
            skip_reading = False
            if speed_profile == "fast":
                skip_reading = True
            elif speed_profile.startswith("custom:"):
                try:
                    parts = speed_profile.split(":")
                    if float(parts[1]) < 15.0:
                        skip_reading = True
                except (ValueError, IndexError):
                    pass

            worker_target = self.worker_target_cache.get(account_id)
            if worker_target is None:
                if target_group_username:
                    try:
                        worker_target = await client.get_entity(target_group_username)
                    except Exception as resolve_err:
                        logger.debug("Failed to resolve target by username for worker %s: %s", active_account.phone, resolve_err)
                if worker_target is None and target_group_tg_id:
                    try:
                        worker_target = await client.get_entity(target_group_tg_id)
                    except Exception as resolve_err:
                        logger.debug("Failed to resolve target by tg_id for worker %s: %s", active_account.phone, resolve_err)
                if worker_target is None:
                    worker_target = target_input

                if isinstance(worker_target, Channel) and getattr(worker_target, "left", False):
                    try:
                        await client(JoinChannelRequest(worker_target))
                        worker_target.left = False
                    except Exception as join_err:
                        logger.debug("JoinChannelRequest info for worker %s: %s", active_account.phone, join_err)

                self.worker_target_cache[account_id] = worker_target

            if not skip_reading:
                await simulate_pre_invite_reading(client, worker_target, mark_read=mark_read)

            if target_username:
                user_to_add = await client.get_entity(target_username)
            elif target_access_hash and target_tg_id:
                user_to_add = InputPeerUser(target_tg_id, target_access_hash)
            elif target_tg_id:
                try:
                    user_to_add = await client.get_entity(target_tg_id)
                except Exception:
                    user_to_add = InputPeerUser(target_tg_id, 0)
            else:
                raise ValueError("Пользователь не найден")

            if isinstance(worker_target, (Chat, InputPeerChat)):
                chat_id = getattr(worker_target, "chat_id", None) or getattr(worker_target, "id", None)
                res = await client(AddChatUserRequest(chat_id=chat_id, user_id=user_to_add, fwd_limit=0))
            else:
                res = await client(InviteToChannelRequest(worker_target, [user_to_add]))

            if isinstance(res, bool):
                invite_success = res
            else:
                missing_uids = set()
                if hasattr(res, "missing_invitees") and isinstance(res.missing_invitees, list) and res.missing_invitees:
                    missing_uids.update(m.user_id for m in res.missing_invitees if hasattr(m, "user_id"))

                target_uid = target_tg_id
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
        except UserChannelsTooMuchError:
            error_status = "channels_too_much"
            error_reason = "Лимит каналов (500/1000)"
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
            async with session_factory() as session:
                db_acc = await session.get(Account, account_id)
                if db_acc:
                    db_acc.status = "cooldown"
                    db_acc.cooldown_until = now + timedelta(seconds=flood.seconds + 30)
                    db_acc.flood_incidents += 1
                    await session.commit()
        except PeerFloodError:
            error_status = "peer_flood"
            error_reason = "Инвайт-бан (PeerFlood)"
            async with session_factory() as session:
                db_acc = await session.get(Account, account_id)
                if db_acc:
                    db_acc.status = "cooldown"
                    db_acc.cooldown_until = now + timedelta(minutes=settings.PEER_FLOOD_COOLDOWN_MINUTES)
                    db_acc.flood_incidents += 1
                    await session.commit()
        except AuthKeyDuplicatedError as dup:
            error_status = "flood_wait"
            error_reason = f"Сессия занята в другом месте: {dup}"
            async with session_factory() as session:
                db_acc = await session.get(Account, account_id)
                if db_acc:
                    db_acc.status = "cooldown"
                    db_acc.cooldown_until = now + timedelta(minutes=settings.PEER_FLOOD_COOLDOWN_MINUTES)
                    db_acc.flood_incidents += 1
                    await session.commit()
        except ChatAdminRequiredError:
            error_status = "no_rights"
            error_reason = "У аккаунта нет прав на добавление участников"
            self.task_excluded_workers.add(account_id)
        except (ChannelInvalidError, ChannelPrivateError, ChatWriteForbiddenError) as chan_err:
            error_status = "channel_forbidden"
            error_reason = f"Аккаунт не имеет доступа к чату или не состоит в нем: {chan_err}"
            self.task_excluded_workers.add(account_id)
        except Exception as unhandled_error:
            error_status = "failed"
            error_reason = str(unhandled_error)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        async with self._db_lock:
            async with session_factory() as session:
                task = await session.get(InviteTask, self.task_id)
                db_member = await session.get(AudienceMember, member_id)
                db_account = await session.get(Account, account_id)
                if db_account:
                    db_account.last_attempt_at = now

                if invite_success:
                    if db_member:
                        db_member.status = "invited"
                        db_member.target_group_id = target_group_id
                        db_member.invited_by_account_id = account_id
                        if db_member.tg_id:
                            hist = (await session.execute(
                                select(AudienceHistory).where(AudienceHistory.tg_id == db_member.tg_id)
                            )).scalars().first()
                            if hist:
                                hist.status = "invited"
                    if db_account:
                        db_account.record_invite(now)
                    if task:
                        task.successful_invites += 1
                        if task.max_invites and task.successful_invites >= task.max_invites:
                            task.status = "completed"
                            task.finished_at = now
                else:
                    if db_member:
                        if error_status in ("account_banned", "flood_wait", "peer_flood", "no_rights", "channel_forbidden"):
                            db_member.status = "pending"
                            db_member.reason = None
                        else:
                            db_member.status = error_status or "failed"
                            db_member.reason = error_reason
                            if db_member.tg_id:
                                hist = (await session.execute(
                                    select(AudienceHistory).where(AudienceHistory.tg_id == db_member.tg_id)
                                )).scalars().first()
                                if hist:
                                    hist.status = db_member.status
                                else:
                                    session.add(AudienceHistory(
                                        tg_id=db_member.tg_id,
                                        username=db_member.username,
                                        first_name=db_member.first_name,
                                        source_chat=getattr(db_member, "source_chat", None) or "unknown",
                                        status=db_member.status
                                    ))
                    if task:
                        if error_status in ("restricted", "channels_too_much"):
                            task.restricted_count += 1
                        elif error_status in ("flood_wait", "peer_flood"):
                            task.flood_errors += 1

                try:
                    await session.commit()
                except asyncio.CancelledError:
                    await session.rollback()
                    raise

        current_line, current_status = await self._record_attempt(
            invite_success=invite_success,
            error_status=error_status,
            error_reason=error_reason,
            account_label=account_label,
            user_tag=user_tag,
        )

        return {
            "success": invite_success,
            "error_status": error_status,
            "error_reason": error_reason,
            "account_label": account_label,
            "user_tag": user_tag,
            "current_line": current_line,
            "current_status": current_status,
        }

    async def _run_sequential(
        self,
        session_factory: Callable[[], AsyncSession],
        progress_callback: Optional[TaskUpdateCallback],
        target_group_data: dict,
        target_input: Any,
        mark_read: bool,
        speed_profile: str,
    ) -> None:
        while not self._stop_event.is_set():
            await self._pause_event.wait()
            if self._stop_event.is_set():
                break

            if self._check_circuit_breaker():
                await self._trip_circuit_breaker(session_factory, progress_callback)
                break

            now = datetime.now(timezone.utc).replace(tzinfo=None)
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
                await auto_recover_cooldowns(session)
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
                            self._build_completion_message(task.max_invites),
                            is_final=True
                        )
                    break

                daily_limit = await get_daily_invite_limit(session=session)
                excluded_ids = await get_excluded_worker_ids(session=session)
                blacklist_enabled = await get_privacy_blacklist_enabled(session=session)
                recent_only = await get_recent_only_enabled(session=session)
                is_sqlite = "sqlite" in settings.DATABASE_URL

                worker_conditions = [
                    Account.is_active == True,
                    Account.status == "active",
                    Account.daily_invites_count < daily_limit,
                ]
                combined_excluded = set(excluded_ids or []) | self.task_excluded_workers
                if combined_excluded:
                    worker_conditions.append(~Account.id.in_(combined_excluded))

                worker_query = select(Account).options(selectinload(Account.proxy)).where(
                    and_(*worker_conditions)
                ).order_by(Account.last_attempt_at.asc().nullsfirst(), Account.id.asc()).limit(1)

                if not is_sqlite:
                    worker_query = worker_query.with_for_update(skip_locked=True)

                active_account = (await session.execute(worker_query)).scalars().first()
                if not active_account:
                    should_continue = await self._handle_no_workers(session_factory, progress_callback, now)
                    if should_continue:
                        continue
                    break

                conditions = [AudienceMember.status == "pending"]
                if blacklist_enabled:
                    restricted_subq = select(AudienceHistory.tg_id).where(
                        AudienceHistory.status.in_(["restricted", "channels_too_much", "uninvitable"])
                    )
                    conditions.append(
                        or_(
                            AudienceMember.tg_id.is_(None),
                            AudienceMember.tg_id.not_in(restricted_subq)
                        )
                    )
                if recent_only:
                    conditions.append(AudienceMember.last_seen_at.is_not(None))

                target_query = select(AudienceMember).where(and_(*conditions)).order_by(AudienceMember.id.asc()).limit(1)
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
                            self._build_queue_finished_message(),
                            is_final=True
                        )
                    break

                chosen_account_id = active_account.id
                chosen_member_id = target_member.id

            res = await self._execute_single_invite(
                session_factory=session_factory,
                account_id=chosen_account_id,
                member_id=chosen_member_id,
                target_group_data=target_group_data,
                target_input=target_input,
                mark_read=mark_read,
                speed_profile=speed_profile,
            )

            if res["error_status"] == "no_rights":
                async with session_factory() as session:
                    task = await session.get(InviteTask, self.task_id)
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

            async with session_factory() as session:
                task = await session.get(InviteTask, self.task_id)
                if task and task.status == "completed":
                    if progress_callback:
                        await progress_callback(
                            self.task_id,
                            task.successful_invites,
                            task.total_targets,
                            task.flood_errors,
                            self._build_completion_message(task.max_invites),
                            is_final=True
                        )
                    break

                if progress_callback and task:
                    await progress_callback(
                        self.task_id,
                        task.successful_invites,
                        task.total_targets,
                        task.flood_errors,
                        res["current_status"],
                        is_final=False
                    )

            pause_time = calculate_delay(speed_profile)
            logger.info(
                "Next invite delay: %.1fs (profile: %s, last_worker: %s, last_status: %s)",
                pause_time,
                speed_profile,
                res["account_label"],
                res["error_status"] or "invited"
            )
            if pause_time > 0:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=pause_time)
                    break
                except asyncio.TimeoutError:
                    pass
            elif self._stop_event.is_set():
                break

    async def _run_sync_batch(
        self,
        session_factory: Callable[[], AsyncSession],
        progress_callback: Optional[TaskUpdateCallback],
        target_group_data: dict,
        target_input: Any,
        mark_read: bool,
        speed_profile: str,
    ) -> None:
        while not self._stop_event.is_set():
            await self._pause_event.wait()
            if self._stop_event.is_set():
                break

            if self._check_circuit_breaker():
                await self._trip_circuit_breaker(session_factory, progress_callback)
                break

            now = datetime.now(timezone.utc).replace(tzinfo=None)
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
                await auto_recover_cooldowns(session)
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
                            self._build_completion_message(task.max_invites),
                            is_final=True
                        )
                    break

                remaining_invites = (task.max_invites - task.successful_invites) if task.max_invites else 999999
                daily_limit = await get_daily_invite_limit(session=session)
                excluded_ids = await get_excluded_worker_ids(session=session)
                blacklist_enabled = await get_privacy_blacklist_enabled(session=session)
                recent_only = await get_recent_only_enabled(session=session)

                worker_conditions = [
                    Account.is_active == True,
                    Account.status == "active",
                    Account.daily_invites_count < daily_limit,
                ]
                combined_excluded = set(excluded_ids or []) | self.task_excluded_workers
                if combined_excluded:
                    worker_conditions.append(~Account.id.in_(combined_excluded))

                available_workers = (await session.execute(
                    select(Account).where(and_(*worker_conditions))
                    .order_by(Account.last_attempt_at.asc().nullsfirst(), Account.id.asc())
                    .limit(remaining_invites)
                )).scalars().all()

                if not available_workers:
                    should_continue = await self._handle_no_workers(session_factory, progress_callback, now)
                    if should_continue:
                        continue
                    break

                batch_size = min(len(available_workers), remaining_invites)
                conditions = [AudienceMember.status == "pending"]
                if blacklist_enabled:
                    restricted_subq = select(AudienceHistory.tg_id).where(
                        AudienceHistory.status.in_(["restricted", "channels_too_much", "uninvitable"])
                    )
                    conditions.append(
                        or_(
                            AudienceMember.tg_id.is_(None),
                            AudienceMember.tg_id.not_in(restricted_subq)
                        )
                    )
                if recent_only:
                    conditions.append(AudienceMember.last_seen_at.is_not(None))

                target_members = (await session.execute(
                    select(AudienceMember).where(and_(*conditions))
                    .order_by(AudienceMember.id.asc())
                    .limit(batch_size)
                )).scalars().all()

                if not target_members:
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
                            self._build_queue_finished_message(),
                            is_final=True
                        )
                    break

                actual_batch_size = min(len(available_workers), len(target_members))
                batch_pairs = list(zip(
                    [w.id for w in available_workers[:actual_batch_size]],
                    [m.id for m in target_members[:actual_batch_size]]
                ))

            coros = [
                self._execute_single_invite(
                    session_factory=session_factory,
                    account_id=acc_id,
                    member_id=mem_id,
                    target_group_data=target_group_data,
                    target_input=target_input,
                    mark_read=mark_read,
                    speed_profile=speed_profile,
                )
                for acc_id, mem_id in batch_pairs
            ]
            results = await asyncio.gather(*coros, return_exceptions=True)

            had_no_rights = False
            latest_status = None
            for res in results:
                if isinstance(res, dict):
                    if res.get("error_status") == "no_rights":
                        had_no_rights = True
                    latest_status = res.get("current_status")

            if had_no_rights:
                async with session_factory() as session:
                    task = await session.get(InviteTask, self.task_id)
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

            async with session_factory() as session:
                task = await session.get(InviteTask, self.task_id)
                if task and task.status == "completed":
                    if progress_callback:
                        await progress_callback(
                            self.task_id,
                            task.successful_invites,
                            task.total_targets,
                            task.flood_errors,
                            self._build_completion_message(task.max_invites),
                            is_final=True
                        )
                    break

                if progress_callback and task:
                    status_to_show = latest_status or self._format_status_text("Завершен залповый раунд инвайтинга")
                    await progress_callback(
                        self.task_id,
                        task.successful_invites,
                        task.total_targets,
                        task.flood_errors,
                        status_to_show,
                        is_final=False
                    )

            pause_time = calculate_delay(speed_profile)
            logger.info(
                "Sync batch pause: %.1fs (profile: %s, batch_size: %d)",
                pause_time,
                speed_profile,
                len(batch_pairs)
            )
            if pause_time > 0:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=pause_time)
                    break
                except asyncio.TimeoutError:
                    pass
            elif self._stop_event.is_set():
                break

    async def _run_parallel_async(
        self,
        session_factory: Callable[[], AsyncSession],
        progress_callback: Optional[TaskUpdateCallback],
        target_group_data: dict,
        target_input: Any,
        mark_read: bool,
        speed_profile: str,
    ) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with session_factory() as session:
            daily_limit = await get_daily_invite_limit(session=session)
            excluded_ids = await get_excluded_worker_ids(session=session)
            worker_conditions = [
                Account.is_active == True,
                Account.status == "active",
                Account.daily_invites_count < daily_limit,
            ]
            combined_excluded = set(excluded_ids or []) | self.task_excluded_workers
            if combined_excluded:
                worker_conditions.append(~Account.id.in_(combined_excluded))

            active_workers = (await session.execute(
                select(Account).where(and_(*worker_conditions))
            )).scalars().all()

            if not active_workers:
                should_continue = await self._handle_no_workers(session_factory, progress_callback, now)
                if not should_continue:
                    return

        in_flight_members: set[int] = set()

        async def worker_loop(worker_account_id: int):
            while not self._stop_event.is_set():
                await self._pause_event.wait()
                if self._stop_event.is_set():
                    break

                if self._check_circuit_breaker():
                    break

                should_exit_worker = False
                claimed_id: Optional[int] = None
                async with self._claim_lock:
                    async with session_factory() as session:
                        task = await session.get(InviteTask, self.task_id)
                        if not task or task.status in ("completed", "paused", "failed"):
                            should_exit_worker = True
                        elif task.max_invites and task.successful_invites >= task.max_invites:
                            task.status = "completed"
                            task.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
                            await session.commit()
                            should_exit_worker = True
                        else:
                            acc = await session.get(Account, worker_account_id)
                            d_limit = await get_daily_invite_limit(session=session)
                            if not acc or acc.status != "active" or not acc.is_active or acc.daily_invites_count >= d_limit or worker_account_id in self.task_excluded_workers:
                                should_exit_worker = True
                            else:
                                blacklist_enabled = await get_privacy_blacklist_enabled(session=session)
                                recent_only = await get_recent_only_enabled(session=session)

                                conditions = [AudienceMember.status == "pending"]
                                if in_flight_members:
                                    conditions.append(~AudienceMember.id.in_(list(in_flight_members)))
                                if blacklist_enabled:
                                    restricted_subq = select(AudienceHistory.tg_id).where(
                                        AudienceHistory.status.in_(["restricted", "channels_too_much", "uninvitable"])
                                    )
                                    conditions.append(
                                        or_(
                                            AudienceMember.tg_id.is_(None),
                                            AudienceMember.tg_id.not_in(restricted_subq)
                                        )
                                    )
                                if recent_only:
                                    conditions.append(AudienceMember.last_seen_at.is_not(None))

                                claimed = (await session.execute(
                                    select(AudienceMember).where(and_(*conditions))
                                    .order_by(AudienceMember.id.asc())
                                    .limit(1)
                                )).scalars().first()

                                if not claimed:
                                    should_exit_worker = True
                                else:
                                    in_flight_members.add(claimed.id)
                                    claimed_id = claimed.id

                if should_exit_worker or claimed_id is None:
                    break

                try:
                    res = await self._execute_single_invite(
                        session_factory=session_factory,
                        account_id=worker_account_id,
                        member_id=claimed_id,
                        target_group_data=target_group_data,
                        target_input=target_input,
                        mark_read=mark_read,
                        speed_profile=speed_profile,
                    )
                finally:
                    async with self._claim_lock:
                        in_flight_members.discard(claimed_id)

                if res["error_status"] == "no_rights":
                    async with session_factory() as session:
                        task = await session.get(InviteTask, self.task_id)
                        if task:
                            task.status = "paused"
                            await session.commit()
                    break

                async with session_factory() as session:
                    task = await session.get(InviteTask, self.task_id)
                    if task and progress_callback:
                        await progress_callback(
                            self.task_id,
                            task.successful_invites,
                            task.total_targets,
                            task.flood_errors,
                            res["current_status"],
                            is_final=(task.status == "completed")
                        )
                    if task and task.status == "completed":
                        break

                pause_time = calculate_delay(speed_profile)
                if pause_time > 0:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=pause_time)
                        break
                    except asyncio.TimeoutError:
                        pass
                elif self._stop_event.is_set():
                    break

        await asyncio.gather(*[worker_loop(w.id) for w in active_workers], return_exceptions=True)

        async with session_factory() as session:
            task = await session.get(InviteTask, self.task_id)
            if task and task.status == "running":
                task.status = "completed"
                task.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
                await session.commit()
                if progress_callback:
                    await progress_callback(
                        self.task_id,
                        task.successful_invites,
                        task.total_targets,
                        task.flood_errors,
                        self._build_queue_finished_message(),
                        is_final=True
                    )

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
            concurrency_mode = getattr(task, "concurrency_mode", None) or self.concurrency_mode or "sequential"
            self.concurrency_mode = concurrency_mode

            target_input = None
            mark_read = True
            if target_group.tg_id and target_group.access_hash:
                target_input = InputPeerChannel(target_group.tg_id, target_group.access_hash)
                mark_read = target_group.chat_type != "channel"
            else:
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

            target_group_data = {
                "id": target_group.id,
                "username": target_group.username,
                "tg_id": target_group.tg_id,
                "chat_type": getattr(target_group, "chat_type", None) or "supergroup",
            }

        # Pre-Sync: discover existing members in target group to eliminate UserAlreadyParticipantError
        async with session_factory() as session:
            try:
                sync_acc = (await session.execute(
                    select(Account).options(selectinload(Account.proxy)).where(
                        Account.is_active == True, Account.status == "active"
                    ).limit(1)
                )).scalars().first()
                if sync_acc and target_input:
                    sync_client = get_telethon_client(sync_acc, proxy=sync_acc.proxy)
                    try:
                        await sync_client.connect()
                        existing_uids = set()
                        participants = sync_client.iter_participants(target_input, limit=10000)
                        if asyncio.iscoroutine(participants):
                            participants = await participants
                        if hasattr(participants, "__aiter__"):
                            async for participant in participants:
                                if participant and getattr(participant, "id", None):
                                    existing_uids.add(participant.id)

                        if existing_uids:
                            uids_list = list(existing_uids)
                            synced_count = 0
                            for i in range(0, len(uids_list), 500):
                                chunk = uids_list[i:i + 500]
                                res = await session.execute(
                                    update(AudienceMember)
                                    .where(
                                        AudienceMember.tg_id.in_(chunk),
                                        AudienceMember.status == "pending"
                                    )
                                    .values(
                                        status="already_participant",
                                        reason="Уже состоит в группе (Pre-Sync)"
                                    )
                                )
                                synced_count += res.rowcount
                            await session.commit()
                            if synced_count > 0:
                                logger.info("Pre-sync: %d members marked as already_participant", synced_count)
                                self.recent_events.insert(0, f"• Pre-Sync: {synced_count} уже в чате")
                                self.recent_events = self.recent_events[:5]
                    except Exception as sync_err:
                        logger.info("Pre-sync participants skipped or restricted: %s", sync_err)
                    finally:
                        await sync_client.disconnect()
            except Exception as exc:
                logger.warning("Error during target pre-sync: %s", exc)

            # Apply privacy blacklist if enabled
            blacklist_enabled = await get_privacy_blacklist_enabled(session=session)
            if blacklist_enabled:
                try:
                    bl_res = await session.execute(
                        update(AudienceMember)
                        .where(
                            AudienceMember.status == "pending",
                            AudienceMember.tg_id.in_(
                                select(AudienceHistory.tg_id).where(
                                    AudienceHistory.status.in_(["restricted", "channels_too_much", "uninvitable"])
                                )
                            )
                        )
                        .values(
                            status="restricted",
                            reason="Исключен блэклистом приватности"
                        )
                    )
                    if bl_res.rowcount > 0:
                        logger.info("Blacklist filter: %d members marked as restricted", bl_res.rowcount)
                        self.recent_events.insert(0, f"• Блэклист: {bl_res.rowcount} закрытых пропущено")
                        self.recent_events = self.recent_events[:5]
                    await session.commit()
                except Exception as bl_err:
                    logger.warning("Error updating blacklist members: %s", bl_err)

        if self.concurrency_mode == "sync_batch":
            await self._run_sync_batch(
                session_factory=session_factory,
                progress_callback=progress_callback,
                target_group_data=target_group_data,
                target_input=target_input,
                mark_read=mark_read,
                speed_profile=speed_profile,
            )
        elif self.concurrency_mode == "parallel_async":
            await self._run_parallel_async(
                session_factory=session_factory,
                progress_callback=progress_callback,
                target_group_data=target_group_data,
                target_input=target_input,
                mark_read=mark_read,
                speed_profile=speed_profile,
            )
        else:
            await self._run_sequential(
                session_factory=session_factory,
                progress_callback=progress_callback,
                target_group_data=target_group_data,
                target_input=target_input,
                mark_read=mark_read,
                speed_profile=speed_profile,
            )

