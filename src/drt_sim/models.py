"""도메인 모델: 요청(Request), 차량(Vehicle), 경로 정류점(RouteStop)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from .geo import Point


class RequestStatus(str, Enum):
    PENDING = "pending"        # 도착했으나 아직 미배차
    ASSIGNED = "assigned"      # 차량에 배차됨
    ONBOARD = "onboard"        # 픽업 완료, 탑승 중
    COMPLETED = "completed"    # 하차 완료
    REJECTED = "rejected"      # 제약 위반으로 배차 실패


@dataclass
class Request:
    """라이더 호출 요청."""

    id: int
    origin: Point
    destination: Point
    request_time: float          # 호출 발생 시각 (초)
    party_size: int = 1          # 동반 인원
    max_wait: float = 300.0      # 최대 허용 대기 (초) — 픽업 시간 윈도우
    max_detour_factor: float = 1.5  # 직선 이동시간 대비 허용 우회 배율

    status: RequestStatus = RequestStatus.PENDING
    assigned_vehicle: Optional[int] = None
    pickup_time: Optional[float] = None
    dropoff_time: Optional[float] = None

    # 분산 메타데이터
    trace_id: str = ""               # 분산 트레이스 묶음 키
    pickup_cell: Optional[str] = None    # 승차지 H3 셀 (소유권 라우팅 키)
    dropoff_cell: Optional[str] = None   # 하차지 H3 셀 (크로스 샤드 판정)
    assigned_at: Optional[float] = None  # 배차 확정 시각 (배차 지연 측정)

    def direct_travel_time(self) -> float:
        from .geo import travel_time_seconds

        return travel_time_seconds(self.origin, self.destination)


class StopType(str, Enum):
    PICKUP = "pickup"
    DROPOFF = "dropoff"


@dataclass
class RouteStop:
    """차량 경로 상의 한 정류점 (특정 요청의 픽업 또는 하차)."""

    request_id: int
    stop_type: StopType
    location: Point
    party_size: int = 1          # 이 정류점이 태우/내리는 인원(정원 검사 정확도)
    # 이 정류점의 요청 제약(경로가 스스로 모든 승객의 대기·우회를 재검증할 수 있게).
    request_time: float = 0.0    # 호출 시각(픽업 대기 검사 기준)
    max_wait: float = 1e9        # 최대 허용 대기(초)
    direct_time: float = 0.0     # 직선 이동시간(우회 검사 기준)
    max_detour: float = 1e9      # 허용 우회 배율
    boarded_at: Optional[float] = None  # 실제 탑승 시각(이미 탄 승객의 우회 재검증용)
    # 계획된 도착 예정 시각 (planning 중 채워짐)
    eta: Optional[float] = None


@dataclass
class Vehicle:
    """합승 차량."""

    id: int
    location: Point
    capacity: int = 4
    route: List[RouteStop] = field(default_factory=list)  # 앞에서부터 방문
    onboard: int = 0                                       # 현재 탑승 인원
    # 분산 소유권 (단일 라이터 + optimistic concurrency)
    home_cell: Optional[str] = None    # 차량의 home 셀 (소유 샤드 결정)
    owner_node: Optional[str] = None   # 현재 단일 라이터 노드
    version: int = 0                   # 낙관적 동시성 버전 (이중 배차 차단)
    # 유휴 리밸런싱: 승객 없는 선이동(deadhead) 목적지. 승객 경로가 생기면 무시/해제된다.
    reposition_target: Optional[Point] = None
    # 누적 통계
    distance_traveled: float = 0.0
    busy_time: float = 0.0

    def is_idle(self) -> bool:
        return not self.route
