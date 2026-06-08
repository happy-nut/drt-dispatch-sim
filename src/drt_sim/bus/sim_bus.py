"""인메모리 시뮬레이션 버스: 네트워크 지연·유실·재정렬·파티션 모델링.

가상시간 런타임 위에서 동작한다. ``publish`` 는 각 구독자에 대해 (시드 기반 RNG 로
뽑은) 지연 후 전달 이벤트를 런타임 힙에 예약한다. 지연 jitter 때문에 메시지 재정렬이
자연스럽게 발생하고, ``loss_prob`` 로 유실을, ``partition`` 집합으로 네트워크 분단을
모델링한다. ``dup_prob`` 으로 at-least-once(중복 전달)도 시연한다.

모든 무작위성은 주입된 ``random.Random`` 에서 나오므로 시드가 같으면 완전 재현된다.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Dict, List, Set

from ..sim_clock import Runtime
from ..tracing import Tracer
from .base import Message, MessageBus


@dataclass
class BusMetrics:
    published: int = 0
    delivered: int = 0
    dropped_loss: int = 0
    dropped_partition: int = 0
    duplicated: int = 0


class SimBus(MessageBus):
    """결정론적 인메모리 메시지 버스."""

    def __init__(
        self,
        runtime: Runtime,
        rng: random.Random,
        tracer: Tracer | None = None,
        base_latency: float = 0.04,
        jitter: float = 0.06,
        loss_prob: float = 0.0,
        dup_prob: float = 0.0,
    ) -> None:
        self._rt = runtime
        self._rng = rng
        self._tracer = tracer
        self.base_latency = base_latency
        self.jitter = jitter
        self.loss_prob = loss_prob
        self.dup_prob = dup_prob

        self._subs: Dict[str, List[str]] = defaultdict(list)
        # 네트워크 파티션: 이 집합에 든 노드는 버스와 단절된다(송수신 모두 차단).
        self.partition: Set[str] = set()
        self.metrics = BusMetrics()
        # 최근 1초간 전달 수(throughput 추정) — (sim_time, count) 슬라이딩은 메트릭에서 처리
        self._delivery_times: List[float] = []

    # --- 구독/발행 ------------------------------------------------------

    def subscribe(self, topic: str, actor_id: str) -> None:
        if actor_id not in self._subs[topic]:
            self._subs[topic].append(actor_id)

    def set_partition(self, nodes: Set[str]) -> None:
        """네트워크 파티션 대상 노드 집합을 설정한다(컨트롤 패널)."""
        self.partition = set(nodes)

    def publish(self, message: Message) -> None:
        self.metrics.published += 1
        # 송신자가 파티션되어 있으면 버스로 나가지 못한다.
        if message.sender and message.sender in self.partition:
            self.metrics.dropped_partition += 1
            return

        for actor_id in self._subs.get(message.topic, []):
            self._deliver_to(actor_id, message)

    def _deliver_to(self, actor_id: str, message: Message) -> None:
        # 수신자가 파티션이면 도달 불가.
        if actor_id in self.partition:
            self.metrics.dropped_partition += 1
            return
        # 유실 모델.
        if self.loss_prob > 0 and self._rng.random() < self.loss_prob:
            self.metrics.dropped_loss += 1
            return

        latency = self.base_latency + self._rng.random() * self.jitter
        self._schedule_delivery(actor_id, message, latency)

        # at-least-once: 확률적 중복 전달(약간 더 늦게 도착, redelivery 플래그).
        if self.dup_prob > 0 and self._rng.random() < self.dup_prob:
            self.metrics.duplicated += 1
            dup = replace(message, redelivery=True)
            self._schedule_delivery(actor_id, dup, latency + self.base_latency)

    def _schedule_delivery(self, actor_id: str, message: Message, latency: float) -> None:
        at = self._rt.now + latency

        def _do() -> None:
            # 전달 시점에 파티션이 생겼을 수도 있으니 한 번 더 확인.
            if actor_id in self.partition:
                self.metrics.dropped_partition += 1
                return
            self.metrics.delivered += 1
            self._delivery_times.append(self._rt.now)
            self._rt.deliver(actor_id, message)

        self._rt.schedule_at(at, _do)

    # --- throughput 추정 ------------------------------------------------

    def delivery_rate(self, window: float = 1.0) -> float:
        """최근 ``window`` 초 동안의 초당 전달 메시지 수."""
        now = self._rt.now
        cutoff = now - window
        # 오래된 기록 정리
        self._delivery_times = [t for t in self._delivery_times if t >= cutoff]
        if window <= 0:
            return 0.0
        return len(self._delivery_times) / window
