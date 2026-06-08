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
    # 누적 통계
    distance_traveled: float = 0.0
    busy_time: float = 0.0

    def is_idle(self) -> bool:
        return not self.route
