"""가상 정류장 스냅 (셔클 특징).

문 앞(door-to-door) 대신, 도보거리와 차량 우회의 총비용을 줄이는 후보 정류장으로
승하차 지점을 스냅한다. 가상 정류장은 운영지역에 균등한 격자로 배치한다(실서비스에서는
실제 승하차 가능 지점 데이터로 대체).

스냅 정책: 라이더 위치에서 ``max_walk_km`` 이내의 가장 가까운 정류장으로 스냅한다.
이내에 정류장이 없으면 도어 위치를 그대로 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ..geo import Point


@dataclass
class VirtualStopSnapper:
    area_km: float
    spacing_km: float = 0.5          # 가상 정류장 격자 간격
    max_walk_km: float = 0.4         # 허용 도보 거리

    def __post_init__(self) -> None:
        self._stops: List[Point] = []
        n = max(1, int(self.area_km / self.spacing_km))
        for i in range(n + 1):
            for j in range(n + 1):
                self._stops.append(
                    Point(self.area_km * i / n, self.area_km * j / n)
                )

    @property
    def stops(self) -> List[Point]:
        return self._stops

    def snap(self, p: Point) -> Point:
        """``p`` 를 도보 허용범위 내 가장 가까운 가상 정류장으로 스냅."""
        best: Optional[Point] = None
        best_d = self.max_walk_km
        for s in self._stops:
            d = p.distance_to(s)
            if d <= best_d:
                best_d = d
                best = s
        return best if best is not None else p
