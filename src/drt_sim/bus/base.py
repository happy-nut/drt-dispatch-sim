"""메시지 버스 추상화: 토픽/스트림 기반 pub-sub.

같은 인터페이스(:class:`MessageBus`)를 두 백엔드가 구현한다:

- :class:`drt_sim.bus.sim_bus.SimBus` — 인메모리, 가상시간 위에서 네트워크 지연·
  유실·재정렬·파티션을 모델링. 결정론적이며 대시보드/테스트의 기본값.
- :class:`drt_sim.bus.redis_bus.RedisBus` — Redis Streams 백엔드(실서비스 옵션).

토픽 상수는 시스템 전반에서 공유한다.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Protocol


# --- 토픽/스트림 이름 ---------------------------------------------------

RIDE_REQUESTS = "ride_requests"        # gateway -> 소유 워커
VEHICLE_TELEMETRY = "vehicle_telemetry"  # 워커 -> 코디네이터/대시보드
ASSIGNMENTS = "assignments"            # 워커 -> (확정된 배차 브로드캐스트)
SHARD_EVENTS = "shard_events"          # 코디네이터 -> 워커 (샤드 할당/회수)
CLUSTER_STATE = "cluster_state"        # 코디네이터(리더) -> 전체 (클러스터 뷰)
HEARTBEAT = "heartbeat"                # 워커 -> 코디네이터 (헬스)
HANDOFF = "handoff"                    # 워커 -> 워커 (크로스 샤드 인지)
SPILL = "spill"                        # 과부하 워커 -> 코디네이터 (백프레셔 spill)


def req_topic(node: str) -> str:
    """소유 워커 노드로 요청을 직접 라우팅하는 토픽 이름."""
    return f"req.{node}"


_MSG_COUNTER = itertools.count(1)


def next_msg_id() -> int:
    """전역 단조 증가 메시지 id (멱등성 dedupe 및 추적용)."""
    return next(_MSG_COUNTER)


@dataclass
class Message:
    """버스 위를 흐르는 봉투(envelope).

    ``msg_id`` 는 at-least-once 전달 하에서 멱등 처리를 위한 키이고,
    ``trace_id`` 는 분산 트레이스를 묶는 키다.
    """

    topic: str
    payload: Any
    sender: str = ""
    msg_id: int = field(default_factory=next_msg_id)
    trace_id: str = ""
    # 같은 논리 메시지의 재전송 여부(at-least-once 시연용)
    redelivery: bool = False


class MessageBus(Protocol):
    """pub-sub 버스 인터페이스. 두 백엔드가 동일하게 구현한다."""

    def subscribe(self, topic: str, actor_id: str) -> None:
        """``actor_id`` 를 ``topic`` 구독자로 등록한다."""
        ...

    def publish(self, message: Message) -> None:
        """메시지를 토픽 구독자들에게 발행한다(전달은 백엔드 정책에 따름)."""
        ...
