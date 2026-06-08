"""도로망 라우팅 (시각화용 경로 형상).

설계 원칙(스펙: "라우팅 최적성에 과투자하지 말 것")
---------------------------------------------------
- **배차 엔진의 통행시간은 빠른 유클리드 근사**를 유지한다(결정론·속도).
- **차량의 화면상 이동 경로 형상만** 도로를 따라가게 한다. 두 정류점 사이를 직선이 아니라
  실제 도로 폴리라인을 따라 그리되, 도착 시각은 엔진이 가정한 유클리드 통행시간에 맞춘다
  (폴리라인 호 길이 기준으로 보간). 따라서 일정·메트릭·결정론은 영향받지 않는다.

두 백엔드를 같은 :class:`Router` 인터페이스로 제공한다:
- :class:`StraightRouter` — 직선(기존 동작). 배치 실험·테스트 기본값(빠르고 결정론적).
- :class:`OSMRouter` — OSMnx 도로 그래프 최단경로(실제 도로 형상). 대시보드용.
  그래프 파일이 없거나 osmnx 미설치면 :func:`load_router` 가 StraightRouter 로 폴백한다.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Protocol, Tuple

from .geo import GeoProjection, Point


class Router(Protocol):
    def path_km(self, a: Point, b: Point) -> List[Point]:
        """a→b 를 잇는 폴리라인(평면 km, 양끝점 포함)."""
        ...


class StraightRouter:
    """직선 라우터 — 기존 동작과 동일(결정론적, 의존성 없음)."""

    def path_km(self, a: Point, b: Point) -> List[Point]:
        return [a, b]


class OSMRouter:
    """OSMnx 도로 그래프 기반 라우터(실제 도로 형상)."""

    def __init__(self, graph, projection: GeoProjection) -> None:
        import osmnx as ox  # 지연 import

        self._ox = ox
        self.G = graph
        self.proj = projection
        self._path_cache: Dict[Tuple[int, int], List[Point]] = {}
        self._nn_cache: Dict[Tuple[float, float], int] = {}

    def _nearest_node(self, p: Point) -> int:
        key = (round(p.x, 2), round(p.y, 2))
        n = self._nn_cache.get(key)
        if n is None:
            lat, lon = self.proj.to_latlon(p)
            n = int(self._ox.nearest_nodes(self.G, lon, lat))
            self._nn_cache[key] = n
        return n

    def path_km(self, a: Point, b: Point) -> List[Point]:
        import networkx as nx

        na, nb = self._nearest_node(a), self._nearest_node(b)
        if na == nb:
            return [a, b]
        key = (na, nb)
        mid = self._path_cache.get(key)
        if mid is None:
            try:
                nodes = nx.shortest_path(self.G, na, nb, weight="length")
                mid = [
                    Point(*self.proj.to_km(self.G.nodes[n]["y"], self.G.nodes[n]["x"]))
                    for n in nodes
                ]
            except Exception:  # 경로 없음 등 → 직선 폴백
                mid = [a, b]
            self._path_cache[key] = mid
        # 실제 좌표가 도로 노드와 약간 떨어져 있으니 양끝을 정확히 붙인다.
        return [a, *mid, b]


def load_router(backend: str, projection: GeoProjection,
                graph_path: Optional[str] = None) -> Router:
    """설정에 따라 라우터를 만든다. 실패 시 StraightRouter 로 폴백."""
    if backend != "osm":
        return StraightRouter()
    if not graph_path:
        return StraightRouter()
    try:
        import osmnx as ox
        graph = ox.load_graphml(graph_path)
        return OSMRouter(graph, projection)
    except Exception:
        # osmnx 미설치 / 그래프 파일 없음 → 직선 폴백(오프라인에서도 동작)
        return StraightRouter()
