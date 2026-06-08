"""대시보드 figure 빌더: 지도 / 큐·throughput 막대 / 지연 차트 / 토폴로지 그래프.

비전공자도 30초 안에 분산 동작을 이해하도록, **소유 노드별 색**을 모든 패널에서
일관되게 쓴다(셀 색 = 차량 색 = 노드 색 = 토폴로지 노드 색).
"""

from __future__ import annotations

import math
from typing import Dict, List

import plotly.graph_objects as go

# 노드별 고정 색 팔레트 (소유권 시각화의 일관성 핵심)
_PALETTE = [
    "#e6194B", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#42d4f4", "#f032e6", "#bfef45", "#fabed4", "#469990",
]
_COORD_COLOR = "#888888"
_DOWN_COLOR = "#2b2b2b"


def node_color(node: str | None) -> str:
    if node is None:
        return "#cccccc"
    if node.startswith("coord"):
        return _COORD_COLOR
    try:
        idx = int(node.split("-")[-1])
    except ValueError:
        idx = abs(hash(node))
    return _PALETTE[idx % len(_PALETTE)]


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def build_map_figure(snap: dict, center_lat: float, center_lon: float,
                     map_style: str = "carto-positron") -> go.Figure:
    """지도 패널 디스패처.

    - ``map_style == "svg"`` → 외부 타일/WebGL 불필요한 벌집 스키매틱(오프라인·폴백).
    - 그 외(``carto-positron``/``open-street-map`` 등) → **실제 도로 타일맵(maplibre)**
      위에 H3 샤드·차량·경로를 얹는다(기본).
    """
    if map_style == "svg":
        return _build_map_svg(snap)
    return _build_map_tiled(snap, center_lat, center_lon, map_style)


def _km_to_latlon(x: float, y: float, clat: float, clon: float, area: float) -> tuple:
    half = area / 2.0
    lat = clat + (y - half) / 111.0
    lon = clon + (x - half) / (111.0 * math.cos(math.radians(clat)))
    return lat, lon


def _build_map_tiled(snap: dict, clat: float, clon: float, map_style: str) -> go.Figure:
    """실제 도로 타일맵(maplibre, 토큰 불필요) 위에 H3 샤드 오버레이."""
    area = snap.get("area_km", 10.0)
    fig = go.Figure()

    # 1) H3 셀 — 소유 노드별로 묶어 None 구분자로 채움(폴리곤 fill)
    by_owner: Dict[str, Dict[str, list]] = {}
    for c in snap["cells"]:
        owner = c["owner"] or "?"
        bucket = by_owner.setdefault(owner, {"lat": [], "lon": []})
        pts = [_km_to_latlon(x, y, clat, clon, area) for x, y in c["boundary_km"]]
        for lat, lon in pts:
            bucket["lat"].append(lat)
            bucket["lon"].append(lon)
        bucket["lat"].append(pts[0][0])
        bucket["lon"].append(pts[0][1])
        bucket["lat"].append(None)
        bucket["lon"].append(None)
    for owner, b in by_owner.items():
        col = node_color(owner)
        fig.add_trace(go.Scattermap(
            lat=b["lat"], lon=b["lon"], mode="lines",
            fill="toself", fillcolor=_hex_to_rgba(col, 0.30),
            line=dict(color=_hex_to_rgba(col, 0.9), width=1),
            name=f"샤드 {owner}", hoverinfo="name",
        ))

    # 2) 차량 경로
    rlat: list = []
    rlon: list = []
    for v in snap["vehicles"]:
        if not v["route_km"]:
            continue
        seq = [(v["x"], v["y"])] + list(v["route_km"])
        for x, y in seq:
            lat, lon = _km_to_latlon(x, y, clat, clon, area)
            rlat.append(lat)
            rlon.append(lon)
        rlat.append(None)
        rlon.append(None)
    if rlat:
        fig.add_trace(go.Scattermap(
            lat=rlat, lon=rlon, mode="lines",
            line=dict(color="rgba(40,40,40,0.45)", width=1.2),
            name="경로", hoverinfo="skip", showlegend=False,
        ))

    # 2b) 유휴 리밸런싱 선이동(점선은 maplibre 미지원 → 가는 실선)
    dlat: list = []
    dlon: list = []
    for v in snap["vehicles"]:
        if v.get("repo"):
            a = _km_to_latlon(v["x"], v["y"], clat, clon, area)
            b = _km_to_latlon(v["repo"][0], v["repo"][1], clat, clon, area)
            dlat += [a[0], b[0], None]
            dlon += [a[1], b[1], None]
    if dlat:
        fig.add_trace(go.Scattermap(
            lat=dlat, lon=dlon, mode="lines",
            line=dict(color="rgba(30,120,200,0.7)", width=2),
            name="유휴 리밸런싱", hoverinfo="skip", showlegend=False,
        ))

    # 3) 차량
    vlat, vlon = [], []
    for v in snap["vehicles"]:
        lat, lon = _km_to_latlon(v["x"], v["y"], clat, clon, area)
        vlat.append(lat)
        vlon.append(lon)
    vcol = [node_color(v["owner"]) for v in snap["vehicles"]]
    vsize = [11 + 3 * v["onboard"] for v in snap["vehicles"]]
    vtext = [f"veh#{v['id']} owner={v['owner']} onboard={v['onboard']} stops={v['stops']}"
             for v in snap["vehicles"]]
    fig.add_trace(go.Scattermap(
        lat=vlat, lon=vlon, mode="markers",
        marker=dict(size=vsize, color=vcol),
        name="차량", text=vtext, hoverinfo="text", showlegend=False,
    ))

    fig.update_layout(
        map=dict(style=map_style, center=dict(lat=clat, lon=clon), zoom=11.3),
        margin=dict(l=0, r=0, t=0, b=0), autosize=True, showlegend=False,
        uirevision="map",
    )
    return fig


def _build_map_svg(snap: dict) -> go.Figure:
    """오프라인 폴백: 외부 타일/WebGL 불필요한 평면 km SVG 벌집."""
    fig = go.Figure()
    by_owner: Dict[str, Dict[str, list]] = {}
    for c in snap["cells"]:
        owner = c["owner"] or "?"
        bucket = by_owner.setdefault(owner, {"x": [], "y": []})
        for x, y in c["boundary_km"]:
            bucket["x"].append(x)
            bucket["y"].append(y)
        first = c["boundary_km"][0]
        bucket["x"].append(first[0])
        bucket["y"].append(first[1])
        bucket["x"].append(None)
        bucket["y"].append(None)
    for owner, pts in by_owner.items():
        col = node_color(owner)
        fig.add_trace(go.Scatter(
            x=pts["x"], y=pts["y"], mode="lines",
            fill="toself", fillcolor=_hex_to_rgba(col, 0.30),
            line=dict(color=_hex_to_rgba(col, 0.9), width=1),
            name=f"샤드 {owner}", hoverinfo="name",
        ))

    rx: list = []
    ry: list = []
    for v in snap["vehicles"]:
        if not v["route_km"]:
            continue
        rx.append(v["x"])
        ry.append(v["y"])
        for x, y in v["route_km"]:
            rx.append(x)
            ry.append(y)
        rx.append(None)
        ry.append(None)
    if rx:
        fig.add_trace(go.Scatter(
            x=rx, y=ry, mode="lines",
            line=dict(color="rgba(40,40,40,0.35)", width=1),
            name="경로", hoverinfo="skip", showlegend=False,
        ))

    dx: list = []
    dy: list = []
    for v in snap["vehicles"]:
        if v.get("repo"):
            dx += [v["x"], v["repo"][0], None]
            dy += [v["y"], v["repo"][1], None]
    if dx:
        fig.add_trace(go.Scatter(
            x=dx, y=dy, mode="lines",
            line=dict(color="rgba(30,120,200,0.55)", width=1.4, dash="dot"),
            name="유휴 리밸런싱", hoverinfo="skip", showlegend=False,
        ))

    vx = [v["x"] for v in snap["vehicles"]]
    vy = [v["y"] for v in snap["vehicles"]]
    vcol = [node_color(v["owner"]) for v in snap["vehicles"]]
    vsize = [10 + 3 * v["onboard"] for v in snap["vehicles"]]
    vtext = [f"veh#{v['id']} owner={v['owner']} onboard={v['onboard']} stops={v['stops']}"
             for v in snap["vehicles"]]
    fig.add_trace(go.Scatter(
        x=vx, y=vy, mode="markers",
        marker=dict(size=vsize, color=vcol, line=dict(color="#fff", width=1.2)),
        name="차량", text=vtext, hoverinfo="text", showlegend=False,
    ))

    area = snap.get("area_km", 10.0)
    fig.update_layout(
        margin=dict(l=0, r=0, t=0, b=0), autosize=True, showlegend=False,
        plot_bgcolor="#f7f7f5", paper_bgcolor="#fff",
        xaxis=dict(visible=False, range=[-0.5, area + 0.5], constrain="domain"),
        yaxis=dict(visible=False, range=[-0.5, area + 0.5],
                   scaleanchor="x", scaleratio=1),
    )
    return fig


def build_queue_figure(snap: dict) -> go.Figure:
    """노드별 큐 깊이 + 처리량 막대."""
    workers = [n for n in snap["nodes"] if n["role"] == "worker"]
    ids = [n["id"] for n in workers]
    colors = [node_color(n["id"]) for n in workers]
    qdepth = [n["queue_depth"] for n in workers]
    proc = [n["processed"] for n in workers]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=ids, y=qdepth, marker_color=colors, name="큐 깊이",
                         text=qdepth, textposition="outside"))
    fig.add_trace(go.Bar(x=ids, y=proc, marker_color="rgba(0,0,0,0.25)", name="누적 처리"))
    fig.update_layout(
        barmode="group", margin=dict(l=30, r=10, t=24, b=24), height=200,
        title=dict(text="노드별 큐 깊이 / 처리량", font=dict(size=12)),
        legend=dict(orientation="h", y=1.25, font=dict(size=9)),
    )
    return fig


def build_latency_figure(snap: dict) -> go.Figure:
    """배차 지연 p50/p95/p99 시계열."""
    tl = snap["latency_timeline"]
    t = [row[0] for row in tl]
    fig = go.Figure()
    for idx, label, col in [(1, "p50", "#3cb44b"), (2, "p95", "#f58231"), (3, "p99", "#e6194B")]:
        fig.add_trace(go.Scatter(x=t, y=[row[idx] for row in tl], mode="lines",
                                 name=label, line=dict(color=col, width=1.5)))
    fig.update_layout(
        margin=dict(l=36, r=10, t=24, b=24), height=200,
        title=dict(text="배차 지연 p50/p95/p99 (초)", font=dict(size=12)),
        legend=dict(orientation="h", y=1.25, font=dict(size=9)),
        uirevision="lat",
    )
    return fig


def build_qdepth_timeline_figure(snap: dict) -> go.Figure:
    """노드별 큐 깊이 시계열(백프레셔/회복 가시화)."""
    tl = snap["qdepth_timeline"]
    t = [row[0] for row in tl]
    fig = go.Figure()
    # 등장한 모든 워커 id 수집
    keys: List[str] = []
    for _ts, d in tl:
        for k in d:
            if k not in keys:
                keys.append(k)
    for k in sorted(keys):
        fig.add_trace(go.Scatter(
            x=t, y=[d.get(k, 0) for _ts, d in tl], mode="lines",
            name=k, line=dict(color=node_color(k), width=1.5),
        ))
    fig.update_layout(
        margin=dict(l=36, r=10, t=24, b=24), height=200,
        title=dict(text="큐 깊이 추이 (백프레셔)", font=dict(size=12)),
        legend=dict(orientation="h", y=1.25, font=dict(size=9)),
        uirevision="qd",
    )
    return fig


def build_topology_elements(snap: dict) -> List[dict]:
    """dash-cytoscape 토폴로지 요소: 코디네이터 + 워커 + 엣지.

    노드는 **고정(preset) 좌표**를 가진다 — 코디네이터는 상단 한 줄, 워커는 하단 한 줄.
    매 갱신마다 레이아웃을 재계산하지 않아 노드가 흔들리지 않는다.
    """
    elements: List[dict] = []
    coords = [n for n in snap["nodes"] if n["role"] == "coordinator"]
    workers = [n for n in snap["nodes"] if n["role"] == "worker"]

    # 기본 zoom=1, pan=0 기준 픽셀 좌표(컨테이너 ~470x230)에 직접 배치 → fit 불필요
    def _positions(items, y, x0=55, x1=420):
        n = max(1, len(items))
        return {it["id"]: {"x": x0 + (i + 0.5) * (x1 - x0) / n, "y": y}
                for i, it in enumerate(items)}

    pos = {}
    pos.update(_positions(coords, 55))    # 코디네이터 상단
    pos.update(_positions(workers, 175))  # 워커 하단

    for n in snap["nodes"]:
        is_leader = n["leader"]
        if not n["alive"]:
            status = "down"
        elif is_leader:
            status = "leader"
        elif n["overloaded"]:
            status = "overloaded"
        else:
            status = n["role"]
        label = n["id"]
        if n["role"] == "worker":
            label += f"\nq={n['queue_depth']} ✓{n['processed']}"
        elif is_leader:
            label += " ★"
        elements.append({
            "data": {"id": n["id"], "label": label, "status": status,
                     "color": node_color(n["id"])},
            "position": pos.get(n["id"], {"x": 280, "y": 130}),
        })

    # 엣지: 리더 코디네이터 -> 각 워커(제어), 코디네이터 간(합의)
    for c in coords:
        for w in workers:
            elements.append({"data": {"source": c["id"], "target": w["id"],
                                      "kind": "control"}})
    for i in range(len(coords)):
        for j in range(i + 1, len(coords)):
            elements.append({"data": {"source": coords[i]["id"], "target": coords[j]["id"],
                                      "kind": "consensus"}})
    return elements
