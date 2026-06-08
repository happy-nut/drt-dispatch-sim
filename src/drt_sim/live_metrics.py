"""실시간 분산 시스템 메트릭 수집.

배차 확정 이벤트(:data:`ASSIGNMENTS`)를 구독해 노드별 throughput·배차 지연을 모으고,
컨트롤러가 매 스텝 큐 깊이·메시지율·부하 불균형(Gini)을 타임라인에 샘플링한다.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Tuple

import numpy as np

from .bus import ASSIGNMENTS, Message, SimBus
from .protocol import AssignmentConfirmed
from .sharding import gini
from .sim_clock import ActorContext


@dataclass
class LiveMetrics:
    """누적·시계열 메트릭 저장소."""

    assign_latencies: Deque[float] = field(default_factory=lambda: deque(maxlen=5000))
    per_node_assigned: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # 시계열: (sim_time, value)
    qdepth_timeline: List[Tuple[float, Dict[str, int]]] = field(default_factory=list)
    msgrate_timeline: List[Tuple[float, float]] = field(default_factory=list)
    latency_timeline: List[Tuple[float, float, float, float]] = field(default_factory=list)
    gini_timeline: List[Tuple[float, float]] = field(default_factory=list)
    recovery_times: List[Tuple[float, str, float]] = field(default_factory=list)  # (t, node, secs)

    def record_assignment(self, ev: AssignmentConfirmed) -> None:
        self.assign_latencies.append(ev.assign_latency)
        self.per_node_assigned[ev.node] += 1

    def percentiles(self) -> Tuple[float, float, float]:
        if not self.assign_latencies:
            return (0.0, 0.0, 0.0)
        arr = np.array(self.assign_latencies)
        return (
            float(np.percentile(arr, 50)),
            float(np.percentile(arr, 95)),
            float(np.percentile(arr, 99)),
        )

    def sample(self, sim_time: float, queue_depths: Dict[str, int], msg_rate: float) -> None:
        self.qdepth_timeline.append((sim_time, dict(queue_depths)))
        self.msgrate_timeline.append((sim_time, msg_rate))
        p50, p95, p99 = self.percentiles()
        self.latency_timeline.append((sim_time, p50, p95, p99))
        if queue_depths:
            self.gini_timeline.append((sim_time, gini(list(queue_depths.values()))))
        # 타임라인 메모리 상한
        for tl in (self.qdepth_timeline, self.msgrate_timeline,
                   self.latency_timeline, self.gini_timeline):
            if len(tl) > 3000:
                del tl[: len(tl) - 3000]


class MetricsActor:
    """ASSIGNMENTS 를 구독해 배차 지연/throughput 을 수집하는 actor."""

    def __init__(self, node_id: str, bus: SimBus, metrics: LiveMetrics) -> None:
        self.node_id = node_id
        self.bus = bus
        self.metrics = metrics

    async def run(self, ctx: ActorContext) -> None:
        self.bus.subscribe(ASSIGNMENTS, self.node_id)
        while True:
            msg: Message = await ctx.recv()
            if isinstance(msg.payload, AssignmentConfirmed) and not msg.redelivery:
                self.metrics.record_assignment(msg.payload)
