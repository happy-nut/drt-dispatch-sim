"""좌표, 거리, 이동시간, 그리고 지리 투영 + H3 셀.

두 좌표계를 함께 다룬다:

- **평면 좌표 ``Point(x, y)``** (단위 km): 배차/라우팅 계산에 쓰는 단순·결정론적 좌표.
  PoC 부터 이어온 내부 표현이다.
- **지리 좌표 (lat, lon)**: 지도 시각화와 **H3 지오 샤딩**에 쓴다. :class:`GeoProjection`
  이 운영지역 중심을 기준으로 평면 km <-> 위경도를 등거리 근사로 변환한다.

실제 서비스에서는 이 모듈을 OSRM/도로망 기반 라우터로 교체하면 된다(README 참고).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import h3

# 기본 평균 주행 속도 (km/h) — 도심 합승 차량 가정
DEFAULT_SPEED_KMH = 24.0

# 위도 1도 ≈ 111 km
_KM_PER_DEG_LAT = 111.0


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


@dataclass(frozen=True)
class GeoProjection:
    """평면 km 좌표 <-> 위경도 변환 + H3 셀 매핑.

    운영지역은 한 변 ``area_km`` 인 정사각형이고, 평면 좌표 (0,0)~(area,area) 의
    중심이 (``center_lat``, ``center_lon``) 에 오도록 등거리 근사로 투영한다.
    """

    center_lat: float
    center_lon: float
    area_km: float
    h3_resolution: int = 7

    def to_latlon(self, p: Point) -> tuple[float, float]:
        half = self.area_km / 2.0
        dlat = (p.y - half) / _KM_PER_DEG_LAT
        km_per_deg_lon = _KM_PER_DEG_LAT * math.cos(math.radians(self.center_lat))
        dlon = (p.x - half) / km_per_deg_lon
        return self.center_lat + dlat, self.center_lon + dlon

    def to_km(self, lat: float, lon: float) -> tuple[float, float]:
        """to_latlon 의 역변환: 위경도 -> 평면 km 좌표 (x, y)."""
        half = self.area_km / 2.0
        km_per_deg_lon = _KM_PER_DEG_LAT * math.cos(math.radians(self.center_lat))
        x = (lon - self.center_lon) * km_per_deg_lon + half
        y = (lat - self.center_lat) * _KM_PER_DEG_LAT + half
        return x, y

    def cell_of(self, p: Point) -> str:
        """평면 좌표가 속한 H3 셀 id."""
        lat, lon = self.to_latlon(p)
        return h3.latlng_to_cell(lat, lon, self.h3_resolution)

    def cells_covering_area(self) -> list[str]:
        """운영지역 정사각형을 덮는 H3 셀 집합(결정론적 정렬)."""
        cells: set[str] = set()
        steps = max(8, int(self.area_km) * 2)
        for i in range(steps + 1):
            for j in range(steps + 1):
                p = Point(self.area_km * i / steps, self.area_km * j / steps)
                cells.add(self.cell_of(p))
        # 경계 셀 누락 방지를 위해 한 겹 확장
        expanded: set[str] = set(cells)
        for c in cells:
            expanded.update(h3.grid_disk(c, 1))
        return sorted(expanded)

    def cell_boundary_latlon(self, cell: str) -> list[tuple[float, float]]:
        """H3 셀 경계 폴리곤 (lat, lon) 정점 리스트(지도 렌더용)."""
        return [(lat, lon) for lat, lon in h3.cell_to_boundary(cell)]

    def cell_center_latlon(self, cell: str) -> tuple[float, float]:
        return h3.cell_to_latlng(cell)
