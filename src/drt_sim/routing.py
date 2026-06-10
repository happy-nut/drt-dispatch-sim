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
        poly = self._path_cache.get(key)
        if poly is None:
            try:
                nodes = nx.shortest_path(self.G, na, nb, weight="length")
                poly = self._geometry_polyline(nodes)
                if not poly:
                    raise ValueError("empty geometry")
            except Exception:  # 경로 없음 등 → 노드 직선 폴백
                poly = [
                    Point(*self.proj.to_km(self.G.nodes[na]["y"], self.G.nodes[na]["x"])),
                    Point(*self.proj.to_km(self.G.nodes[nb]["y"], self.G.nodes[nb]["x"])),
                ]
            self._path_cache[key] = poly
        # 양끝을 raw 지점이 아니라 도로 노드에 스냅(=poly 자체)해서, 도로에서 떨어진
        # 승하차 지점으로 직선으로 튀는 connector 를 없앤다. 전체가 도로 형상만 따른다.
        return poly

    def _geometry_polyline(self, nodes: List[int]) -> List[Point]:
        """경로 노드 사이를 **엣지 geometry(실제 도로 곡선)**로 잇는다.

        simplify 된 그래프에서 교차점 노드만 직선으로 이으면 도로 곡선을 무시해
        강·블록을 가로지른다. 엣지에 저장된 LineString 좌표를 펼쳐 도로를 따라간다.
        """
        pts: List[Point] = []
        for u, w in zip(nodes[:-1], nodes[1:]):
            data = self.G.get_edge_data(u, w) or {}
            edge = min(data.values(), key=lambda e: e.get("length", 1.0)) if data else {}
            geom = edge.get("geometry")
            if geom is not None:
                coords = list(geom.coords)  # shapely LineString: (lon, lat)
            else:  # 직선 엣지(교차점 인접) — 양 끝 노드만
                coords = [
                    (self.G.nodes[u]["x"], self.G.nodes[u]["y"]),
                    (self.G.nodes[w]["x"], self.G.nodes[w]["y"]),
                ]
            for lon, lat in coords:
                p = Point(*self.proj.to_km(lat, lon))
                if not pts or pts[-1].x != p.x or pts[-1].y != p.y:
                    pts.append(p)
        return pts


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
