"""Wall-clock asyncio 런타임 — 가상시간 :class:`Runtime` 의 drop-in 대체.

같은 actor 코드(`async def run(ctx)`, `await ctx.sleep`, `await ctx.recv`)를 **진짜
asyncio 이벤트 루프** 위에서 돌린다. actor 가 특정 런타임에 묶이지 않았음을 증명하고,
"진짜 분산"(실제 OS 프로세스) 모드의 노드 내부 실행 엔진으로 쓰인다.

:class:`Runtime` 과 동일한 표면을 노출한다: ``now``, ``schedule_at``, ``deliver``,
``spawn``, ``kill``, ``is_alive``. 따라서 :class:`drt_sim.bus.SimBus` 가 수정 없이 이
런타임 위에서도 동작한다(버스는 ``runtime.now/schedule_at/deliver`` 만 사용).

``time_scale`` 로 배속 실행이 가능하다(가상초 dt → wall dt/time_scale). wall-clock
기반이라 결정론은 보장되지 않는다(결정론적 실험은 가상시간 :class:`Runtime` 사용).
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine, Dict, List, Tuple


class AsyncActorContext:
    """asyncio 백엔드용 actor 컨텍스트. :class:`ActorContext` 와 같은 메서드 표면."""

    def __init__(self, runtime: "AsyncioRuntime", actor_id: str) -> None:
        self._rt = runtime
        self.actor_id = actor_id
        self.inbox: asyncio.Queue = asyncio.Queue()

    @property
    def now(self) -> float:
        return self._rt.now

    async def sleep(self, dt: float) -> None:
        await asyncio.sleep(max(0.0, dt) / self._rt.time_scale)

    async def recv(self) -> Any:
        return await self.inbox.get()

    def try_recv(self) -> Any:
        try:
            return self.inbox.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def inbox_depth(self) -> int:
        return self.inbox.qsize()

    def call_later(self, dt: float, fn: Callable[[], None]) -> None:
        self._rt.schedule_at(self._rt.now + dt, fn)


class AsyncioRuntime:
    """실제 asyncio 이벤트 루프 기반 런타임."""

    def __init__(self, time_scale: float = 1.0) -> None:
        self.time_scale = time_scale
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start: float = 0.0
        self._contexts: Dict[str, AsyncActorContext] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._alive: Dict[str, bool] = {}
        self._pending_spawns: List[Tuple[str, Callable[[AsyncActorContext], Coroutine]]] = []
        self._pending_sched: List[Tuple[float, Callable[[], None]]] = []

    # --- 시계 ----------------------------------------------------------

    @property
    def now(self) -> float:
        if self._loop is None:
            return 0.0
        return (self._loop.time() - self._start) * self.time_scale

    # --- 이벤트 스케줄링 -----------------------------------------------

    def schedule_at(self, at: float, thunk: Callable[[], None]) -> None:
        if self._loop is None:
            self._pending_sched.append((at, thunk))
            return
        delay = max(0.0, (at - self.now) / self.time_scale)
        self._loop.call_later(delay, thunk)

    # --- 메시지 전달 ----------------------------------------------------

    def deliver(self, actor_id: str, msg: Any) -> None:
        if not self._alive.get(actor_id, False):
            return
        ctx = self._contexts.get(actor_id)
        if ctx is not None:
            ctx.inbox.put_nowait(msg)

    # --- actor 수명주기 -------------------------------------------------

    def spawn(self, actor_id: str,
              make_coro: Callable[[AsyncActorContext], Coroutine]) -> AsyncActorContext:
        ctx = AsyncActorContext(self, actor_id)
        self._contexts[actor_id] = ctx
        self._alive[actor_id] = True
        if self._loop is None:
            self._pending_spawns.append((actor_id, make_coro))
        else:
            self._tasks[actor_id] = self._loop.create_task(
                self._guard(actor_id, make_coro(ctx))
            )
        return ctx

    def kill(self, actor_id: str) -> None:
        self._alive[actor_id] = False
        task = self._tasks.pop(actor_id, None)
        if task is not None:
            task.cancel()
        ctx = self._contexts.get(actor_id)
        if ctx is not None:
            while True:
                try:
                    ctx.inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break

    def is_alive(self, actor_id: str) -> bool:
        return self._alive.get(actor_id, False)

    async def _guard(self, actor_id: str, coro: Coroutine) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            pass

    # --- 실행 ----------------------------------------------------------

    async def run_for(self, wall_seconds: float) -> None:
        """이벤트 루프를 ``wall_seconds`` (실제 초)만큼 돌린다."""
        self._loop = asyncio.get_running_loop()
        self._start = self._loop.time()
        for at, thunk in self._pending_sched:
            self.schedule_at(at, thunk)
        self._pending_sched.clear()
        for actor_id, make_coro in self._pending_spawns:
            ctx = self._contexts[actor_id]
            self._tasks[actor_id] = self._loop.create_task(
                self._guard(actor_id, make_coro(ctx))
            )
        self._pending_spawns.clear()
        await asyncio.sleep(wall_seconds)
