"""좌표, 거리, 이동시간 계산.

PoC에서는 도로망 대신 평면(유클리드) 좌표계를 사용해 이동시간을 근사한다.
좌표 단위는 km로 간주하고, 고정 평균 속도로 이동시간(초)을 계산한다.
실제 서비스에서는 이 모듈을 OSRM/도로망 기반 라우터로 교체하면 된다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# 기본 평균 주행 속도 (km/h) — 도심 합승 차량 가정
DEFAULT_SPEED_KMH = 24.0


@dataclass(frozen=True)
class Point:
    """평면 좌표 (단위: km)."""

    x: float
    y: float

    def distance_to(self, other: "Point") -> float:
        """유클리드 거리 (km)."""
        return math.hypot(self.x - other.x, self.y - other.y)


def travel_time_seconds(a: Point, b: Point, speed_kmh: float = DEFAULT_SPEED_KMH) -> float:
    """두 점 사이 이동시간(초). 속도가 0 이하이면 ValueError."""
    if speed_kmh <= 0:
        raise ValueError("speed_kmh must be positive")
    distance_km = a.distance_to(b)
    return distance_km / speed_kmh * 3600.0
