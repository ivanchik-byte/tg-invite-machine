import asyncio
import time
from typing import Optional, Any, Coroutine

class InviteTaskManager:
    """Manages the lifecycle and state of the active background invite task."""

    def __init__(self) -> None:
        self.active_task_id: Optional[int] = None
        self.active_orchestrator: Optional[Any] = None
        self.active_task_handle: Optional[asyncio.Task] = None
        self.last_ui_update_time: float = 0.0

    def is_running(self) -> bool:
        if self.active_task_handle and not self.active_task_handle.done():
            return True
        return False

    def get_active_task_id(self) -> Optional[int]:
        return self.active_task_id if self.is_running() else None

    def start(self, task_id: int, orchestrator: Any, coro: Coroutine) -> asyncio.Task:
        if self.is_running():
            raise RuntimeError(f"Task #{self.active_task_id} is already running.")

        self.active_task_id = task_id
        self.active_orchestrator = orchestrator
        self.last_ui_update_time = 0.0

        task = asyncio.create_task(coro)
        self.active_task_handle = task
        task.add_done_callback(self._on_task_finished)
        return task

    def stop(self) -> bool:
        if self.active_orchestrator:
            self.active_orchestrator.stop()
            handle = self.active_task_handle
            self.active_task_id = None
            self.active_orchestrator = None
            self.active_task_handle = None
            # interrupt active loop if currently suspended in asyncio.sleep
            if handle and not handle.done():
                handle.cancel()
            return True
        return False

    def pause(self) -> bool:
        if self.active_orchestrator:
            self.active_orchestrator.pause()
            return True
        return False

    def resume(self) -> bool:
        if self.active_orchestrator:
            self.active_orchestrator.resume()
            return True
        return False

    def should_update_ui(self, min_interval: float = 3.0, is_final: bool = False) -> bool:
        now = time.monotonic()
        if not is_final and (now - self.last_ui_update_time < min_interval):
            return False
        self.last_ui_update_time = now
        return True

    def _on_task_finished(self, task: asyncio.Task) -> None:
        if task is self.active_task_handle:
            self.active_task_id = None
            self.active_orchestrator = None
            self.active_task_handle = None

invite_task_manager = InviteTaskManager()
