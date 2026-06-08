"""결정론적 가상시간(virtual-time) 협조적 async 런타임.

이 모듈이 시뮬레이터 전체의 백본이다. 실제 wall-clock asyncio 이벤트 루프 대신,
시드 기반으로 **완전히 재현 가능한** 커스텀 스케줄러를 제공한다.

설계 요지
---------
- 모든 actor 는 진짜 ``async def run(self, ctx)`` 코루틴이다. actor 는
  ``await ctx.sleep(dt)`` 로 가상시간을 진전시키며 기다리고,
  ``msg = await ctx.recv()`` 로 자기 인박스에 메시지가 올 때까지 블록된다.
- 블로킹 호출이 없다(요구사항). 모든 대기는 스케줄러가 관리하는 가상시간 이벤트로
  변환된다.
- 이벤트는 ``(time, seq)`` 로 전순서가 매겨진 최소 힙에 들어간다. ``seq`` 는 전역
  단조 증가 카운터다. 코드 실행 순서와 시드 기반 RNG 가 결정론적이므로 전체
  시뮬레이션이 재현 가능하다.

이 위에 메시지 버스(:mod:`drt_sim.bus`)가 지연/유실/재정렬을 가상시간 이벤트로
모델링하고, 게이트웨이·코디네이터·워커 actor 가 동작한다.
"""

from __future__ import annotations

import heapq
import types
from collections import defaultdict, deque
from typing import Any, Callable, Coroutine, Deque, Dict, List, Optional, Tuple


@types.coroutine
def _suspend(cmd: Tuple[str, Any]):
    """actor 코루틴을 스케줄러로 양보(yield)시키는 저수준 awaitable.

    actor 가 ``await _suspend(('sleep', dt))`` 를 호출하면 명령 튜플이 런타임으로
    전달되고, 런타임이 코루틴을 재개할 때 보낸 값이 ``await`` 의 결과로 돌아온다.
    """
    return (yield cmd)


class ActorContext:
    """actor 가 런타임과 상호작용하는 핸들.

    각 actor 는 자신의 ``ActorContext`` 를 통해서만 시간을 진전시키거나 메시지를
    주고받는다. actor 코드가 런타임 내부에 직접 접근하지 못하게 하는 얇은 경계.
    """

    def __init__(self, runtime: "Runtime", actor_id: str) -> None:
        self._rt = runtime
        self.actor_id = actor_id

    @property
    def now(self) -> float:
        """현재 가상시간(초)."""
        return self._rt.now

    async def sleep(self, dt: float) -> None:
        """가상시간으로 ``dt`` 초 동안 대기한다."""
        if dt < 0:
            raise ValueError("sleep dt must be >= 0")
        await _suspend(("sleep", dt))

    async def recv(self) -> Any:
        """자기 인박스에 메시지가 도착할 때까지 블록하고 그 메시지를 반환한다."""
        return await _suspend(("recv", None))

    def try_recv(self) -> Optional[Any]:
        """블록하지 않고 인박스에서 메시지를 하나 꺼낸다. 없으면 None."""
        inbox = self._rt._inboxes[self.actor_id]
        return inbox.popleft() if inbox else None

    def inbox_depth(self) -> int:
        """현재 인박스에 쌓인 메시지 수(백프레셔 측정용)."""
        return len(self._rt._inboxes[self.actor_id])

    def call_later(self, dt: float, fn: Callable[[], None]) -> None:
        """``dt`` 초 뒤에 콜백을 실행하도록 예약한다(actor 외부 로직용)."""
        self._rt.schedule_at(self._rt.now + dt, fn)


class Runtime:
    """가상시간 이벤트 스케줄러 + actor 드라이버.

    하나의 시뮬레이션은 하나의 ``Runtime`` 위에서 돈다. 단일 스레드·단일 이벤트
    큐이므로 동시성 버그가 없고 재현 가능하다.
    """

    def __init__(self) -> None:
        self.now: float = 0.0
        self._heap: List[Tuple[float, int, Callable[[], None]]] = []
        self._seq: int = 0
        self._inboxes: Dict[str, Deque[Any]] = defaultdict(deque)
        self._waiting: Dict[str, bool] = {}  # recv 로 블록된 actor 집합
        self._coros: Dict[str, Coroutine] = {}
        self._contexts: Dict[str, ActorContext] = {}
        self._alive: Dict[str, bool] = {}

    # --- 이벤트 스케줄링 -------------------------------------------------

    def schedule_at(self, at: float, thunk: Callable[[], None]) -> None:
        """가상시간 ``at`` 에 실행할 콜백을 힙에 넣는다."""
        if at < self.now:
            at = self.now  # 과거로는 예약 불가 — 현재로 클램프
        heapq.heappush(self._heap, (at, self._seq, thunk))
        self._seq += 1

    # --- actor 수명주기 -------------------------------------------------

    def spawn(self, actor_id: str, make_coro: Callable[[ActorContext], Coroutine]) -> ActorContext:
        """actor 코루틴을 등록하고 즉시 첫 스텝을 예약한다.

        ``make_coro`` 는 ``ctx`` 를 받아 코루틴(``actor.run(ctx)``)을 돌려주는 팩토리다.
        """
        ctx = ActorContext(self, actor_id)
        self._contexts[actor_id] = ctx
        self._coros[actor_id] = make_coro(ctx)
        self._alive[actor_id] = True
        self.schedule_at(self.now, lambda: self._step(actor_id, None))
        return ctx

    def kill(self, actor_id: str) -> None:
        """actor 를 즉시 중단시킨다(노드 장애 주입). 인박스도 비운다.

        장애 후 재가입을 위해 ``spawn`` 으로 같은 id 를 다시 띄울 수 있다.
        """
        self._alive[actor_id] = False
        self._coros.pop(actor_id, None)
        self._waiting.pop(actor_id, None)
        self._inboxes[actor_id].clear()

    def is_alive(self, actor_id: str) -> bool:
        return self._alive.get(actor_id, False)

    # --- 메시지 전달 ----------------------------------------------------

    def deliver(self, actor_id: str, msg: Any) -> None:
        """메시지를 actor 인박스에 넣고, 블록 중이면 깨운다.

        버스가 (지연 후) 호출한다. 죽은 actor 에게는 조용히 버린다.
        """
        if not self._alive.get(actor_id, False):
            return
        self._inboxes[actor_id].append(msg)
        if self._waiting.pop(actor_id, False):
            inbox = self._inboxes[actor_id]
            m = inbox.popleft()
            self.schedule_at(self.now, lambda: self._step(actor_id, m))

    # --- 코루틴 드라이버 -------------------------------------------------

    def _step(self, actor_id: str, value: Any) -> None:
        """actor 코루틴을 한 스텝 진행시키고 yield 된 명령을 해석한다."""
        if not self._alive.get(actor_id, False):
            return
        coro = self._coros.get(actor_id)
        if coro is None:
            return
        try:
            cmd = coro.send(value)
        except StopIteration:
            self._coros.pop(actor_id, None)
            return
        except Exception:  # pragma: no cover - actor 버그는 시뮬레이션을 멈춰야 한다
            self._coros.pop(actor_id, None)
            raise

        op = cmd[0]
        if op == "sleep":
            dt = cmd[1]
            self.schedule_at(self.now + dt, lambda: self._step(actor_id, None))
        elif op == "recv":
            inbox = self._inboxes[actor_id]
            if inbox:
                m = inbox.popleft()
                self.schedule_at(self.now, lambda: self._step(actor_id, m))
            else:
                self._waiting[actor_id] = True
        else:  # pragma: no cover
            raise RuntimeError(f"unknown actor command: {op!r}")

    # --- 실행 루프 ------------------------------------------------------

    def run_until(self, t: float) -> None:
        """가상시간 ``t`` 까지(포함) 모든 이벤트를 처리하고 시계를 ``t`` 로 맞춘다.

        대시보드는 이 메서드를 wall-clock 페이스에 맞춰 작은 증분으로 반복 호출해
        애니메이션을 만든다. 배치 실행은 큰 ``t`` 한 번으로 끝낸다.
        """
        while self._heap and self._heap[0][0] <= t:
            at, _seq, thunk = heapq.heappop(self._heap)
            self.now = at
            thunk()
        if t > self.now:
            self.now = t

    def run_forever(self, max_time: float) -> None:
        """이벤트가 없어질 때까지(또는 ``max_time`` 까지) 처리한다."""
        while self._heap and self._heap[0][0] <= max_time:
            at, _seq, thunk = heapq.heappop(self._heap)
            self.now = at
            thunk()
        self.now = max(self.now, min(max_time, self.now) if self._heap else max_time)

    @property
    def pending_events(self) -> int:
        return len(self._heap)
