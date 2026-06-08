"""분산 트레이싱 & 구조화 이벤트 로그.

요청 하나가 gateway -> bus -> worker -> store 를 거치는 경로를 trace_id 로 묶어
추적한다. 대시보드의 실시간 이벤트 로그 스트림과 추적(trace) 패널이 이 버퍼를 읽는다.

가상시간 기반이므로 모든 타임스탬프는 시뮬레이션 시각(초)이다.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional


@dataclass(frozen=True)
class TraceEvent:
    """트레이스 스팬 한 줄. 한 요청의 생애 중 한 단계를 기록한다."""

    sim_time: float
    trace_id: str
    node: str          # 이벤트를 낸 노드 (gateway / coord / worker-2 / store ...)
    kind: str          # request_emitted / routed / assigned / rejected / handoff ...
    detail: str = ""
    request_id: Optional[int] = None

    def render(self) -> str:
        rid = f" req#{self.request_id}" if self.request_id is not None else ""
        return f"[{self.sim_time:8.1f}s] {self.node:<10} {self.kind:<16}{rid} {self.detail}"


class Tracer:
    """링버퍼 기반 트레이스 수집기.

    actor 들이 ``tracer.emit(...)`` 로 스팬을 남기고, 대시보드/리포트가 최근 N 개를
    읽어간다. 메모리 사용을 제한하기 위해 고정 크기 deque 를 쓴다.
    """

    def __init__(self, capacity: int = 4000) -> None:
        self._events: Deque[TraceEvent] = deque(maxlen=capacity)
        # 요청별 스팬 인덱스 (분산 트레이스 조회용)
        self._by_request: Dict[int, List[TraceEvent]] = {}

    def emit(
        self,
        sim_time: float,
        trace_id: str,
        node: str,
        kind: str,
        detail: str = "",
        request_id: Optional[int] = None,
    ) -> None:
        ev = TraceEvent(sim_time, trace_id, node, kind, detail, request_id)
        self._events.append(ev)
        if request_id is not None:
            self._by_request.setdefault(request_id, []).append(ev)

    def recent(self, n: int = 50) -> List[TraceEvent]:
        """가장 최근 n 개 이벤트(오래된 것 -> 최신 순)."""
        if n >= len(self._events):
            return list(self._events)
        return list(self._events)[-n:]

    def trace(self, request_id: int) -> List[TraceEvent]:
        """특정 요청의 전체 분산 트레이스(gateway->...->store)."""
        return list(self._by_request.get(request_id, []))

    def __len__(self) -> int:
        return len(self._events)
