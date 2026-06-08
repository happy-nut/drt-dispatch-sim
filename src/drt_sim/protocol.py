"""노드 간 메시지 페이로드 정의.

:class:`drt_sim.bus.Message` 의 ``payload`` 로 실어 나르는 구조체들. sim 백엔드에서는
객체 그대로, redis 백엔드에서는 JSON 직렬화되어 전달된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .sharding import ShardMap


@dataclass
class Heartbeat:
    """워커 -> 코디네이터 헬스 신호."""

    node: str
    sim_time: float
    queue_depth: int            # 미처리 요청 백로그(백프레셔 지표)
    processed: int              # 누적 처리 건수
    overloaded: bool = False    # 고수위 초과 여부


@dataclass
class ClusterView:
    """코디네이터(리더) -> 전체. 클러스터의 권위 있는 뷰(결과적 일관성).

    ``version`` 으로 staleness 를 드러낸다. 노드들은 최신 뷰를 보고 라우팅/소유권을
    판단하지만, 전파 지연 때문에 일시적으로 서로 다른 버전을 볼 수 있다.
    """

    version: int
    sim_time: float
    leader: str
    shard_map: ShardMap
    alive_workers: List[str] = field(default_factory=list)
    down_workers: List[str] = field(default_factory=list)
    queue_depths: Dict[str, int] = field(default_factory=dict)


@dataclass
class ShardMigration:
    """샤드 마이그레이션 알림(지도 애니메이션 트리거)."""

    version: int
    sim_time: float
    cell: str
    old_owner: Optional[str]
    new_owner: Optional[str]
    reason: str  # "node_failure" | "node_join" | "rebalance"


@dataclass
class Handoff:
    """크로스 샤드 인지: 픽업 샤드 노드 -> 하차 샤드 노드."""

    request_id: int
    from_node: str
    to_node: str
    dropoff_cell: str
    sim_time: float


@dataclass
class AssignmentConfirmed:
    """확정 배차 브로드캐스트(멱등성 dedupe + 메트릭용)."""

    request_id: int
    vehicle_id: int
    node: str
    sim_time: float
    assign_latency: float  # 요청 도착 -> 배차 확정 (초)
