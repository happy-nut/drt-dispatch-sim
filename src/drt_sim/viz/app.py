"""Plotly Dash 대시보드.

동기화된 두 패널 + 컨트롤:
- **지도 패널**: H3 셀을 소유 노드색으로, 차량/경로를 실시간 표시. 노드가 죽으면 셀 색이
  다시 칠해지며 샤드 마이그레이션이 보인다.
- **클러스터 패널**: dash-cytoscape 토폴로지(리더 배지·헬스), 큐 깊이/처리량, 배차 지연
  p50/p95/p99, 큐 추이, 실시간 트레이스 로그.
- **컨트롤**: 노드 장애/리더 장애/파티션/수요 폭증/워커 추가, 재생·배속.

약 6Hz 로 갱신되며 시뮬레이션 가상 시계와 동기화된다.
"""

from __future__ import annotations

import dash
import dash_cytoscape as cyto
from dash import Input, Output, State, dcc, html

from ..config import SimConfig
from .figures import (
    build_latency_figure,
    build_map_figure,
    build_qdepth_timeline_figure,
    build_queue_figure,
    build_topology_elements,
)
from .runner import SimRunner

_CYTO_STYLESHEET = [
    {"selector": "node", "style": {
        "label": "data(label)", "background-color": "data(color)",
        "width": 46, "height": 46, "font-size": 9, "color": "#fff",
        "text-valign": "center", "text-halign": "center", "text-wrap": "wrap",
        "text-outline-color": "#333", "text-outline-width": 1.5,
    }},
    {"selector": '[status = "leader"]', "style": {
        "border-color": "#FFD700", "border-width": 5, "shape": "round-rectangle"}},
    {"selector": '[status = "coordinator"]', "style": {"shape": "round-rectangle"}},
    {"selector": '[status = "overloaded"]', "style": {
        "border-color": "#e6194B", "border-width": 5}},
    {"selector": '[status = "down"]', "style": {
        "background-color": "#2b2b2b", "opacity": 0.4, "border-color": "#e6194B",
        "border-width": 2}},
    {"selector": '[kind = "control"]', "style": {
        "line-color": "#bbb", "width": 1, "curve-style": "bezier",
        "target-arrow-shape": "triangle", "target-arrow-color": "#bbb",
        "arrow-scale": 0.6}},
    {"selector": '[kind = "consensus"]', "style": {
        "line-color": "#FFD700", "width": 2, "line-style": "dashed",
        "curve-style": "bezier"}},
]

_CARD = {"backgroundColor": "#fff", "borderRadius": "8px", "padding": "8px",
         "boxShadow": "0 1px 3px rgba(0,0,0,0.12)", "marginBottom": "8px"}


def _badge(label: str, value_id: str, color: str = "#333"):
    return html.Div([
        html.Div(label, style={"fontSize": "10px", "color": "#888"}),
        html.Div(id=value_id, style={"fontSize": "16px", "fontWeight": "bold", "color": color}),
    ], style={"padding": "2px 12px", "borderRight": "1px solid #eee"})


def create_app(config: SimConfig) -> dash.Dash:
    runner = SimRunner(config)
    runner.start()
    app = dash.Dash(__name__, title="DRT 분산 배차 엔진")
    worker0 = "worker-0"

    app.layout = html.Div([
        dcc.Interval(id="tick", interval=160, n_intervals=0),
        dcc.Store(id="noop"),

        # 헤더 + 메트릭 배지
        html.Div([
            html.Div("🚐 실시간 수요응답형 합승 배차 엔진 — 분산 클러스터",
                     style={"fontSize": "18px", "fontWeight": "bold", "color": "#fff",
                            "padding": "8px 16px"}),
            html.Div([
                _badge("가상시각(s)", "b-time", "#1a73e8"),
                _badge("리더", "b-leader", "#FFD700"),
                _badge("샤드맵 ver", "b-shard"),
                _badge("요청(배차/완료)", "b-req"),
                _badge("거절", "b-rej", "#e6194B"),
                _badge("지연 p95", "b-p95"),
                _badge("복구시간", "b-recovery", "#3cb44b"),
                _badge("이중배차 차단", "b-blocked", "#e6194B"),
                _badge("msg/s", "b-msgrate"),
            ], style={"display": "flex", "backgroundColor": "#fff", "padding": "4px",
                      "borderRadius": "6px", "margin": "0 12px"}),
        ], style={"backgroundColor": "#202124", "paddingBottom": "8px"}),

        # 컨트롤 바
        html.Div([
            html.Button("⏸ 일시정지", id="btn-play", n_clicks=0, style={"marginRight": "8px"}),
            html.Span("배속", style={"fontSize": "11px", "margin": "0 4px"}),
            html.Div(dcc.Slider(id="speed", min=0.25, max=8, step=0.25, value=1.0,
                                marks={1: "1x", 4: "4x", 8: "8x"}),
                     style={"width": "180px", "display": "inline-block",
                            "verticalAlign": "middle"}),
            html.Span(" │ ", style={"color": "#ccc"}),
            dcc.Dropdown(id="worker-pick", options=[], value=worker0,
                         clearable=False,
                         style={"width": "130px", "display": "inline-block",
                                "verticalAlign": "middle", "fontSize": "12px"}),
            html.Button("💀 워커 장애", id="btn-kill-worker", n_clicks=0,
                        style={"margin": "0 4px"}),
            html.Button("👑 리더 장애", id="btn-kill-leader", n_clicks=0,
                        style={"margin": "0 4px"}),
            html.Button("📈 수요 폭증", id="btn-surge", n_clicks=0, style={"margin": "0 4px"}),
            html.Button("✂ 파티션(worker-0)", id="btn-partition", n_clicks=0,
                        style={"margin": "0 4px"}),
            html.Button("🔗 파티션 해제", id="btn-heal", n_clicks=0, style={"margin": "0 4px"}),
            html.Button("➕ 워커 추가", id="btn-add", n_clicks=0, style={"margin": "0 4px"}),
        ], style={"padding": "8px 16px", "backgroundColor": "#f1f3f4",
                  "display": "flex", "alignItems": "center"}),

        # 본문: 좌(지도) + 우(클러스터)
        html.Div([
            html.Div([
                html.Div("운영지역 · H3 지오 샤드 (색 = 소유 노드)",
                         style={"fontSize": "12px", "fontWeight": "bold", "padding": "4px"}),
                dcc.Graph(id="map", style={"height": "640px", "width": "100%"},
                          config={"displayModeBar": False, "responsive": True}),
            ], style={**_CARD, "width": "58%", "marginRight": "8px"}),

            html.Div([
                html.Div([
                    html.Div("클러스터 토폴로지 (★=리더, 빨강=과부하/장애)",
                             style={"fontSize": "12px", "fontWeight": "bold"}),
                    cyto.Cytoscape(
                        id="topology", layout={"name": "preset", "fit": False},
                        style={"width": "100%", "height": "230px"},
                        stylesheet=_CYTO_STYLESHEET, elements=[],
                        zoom=1, pan={"x": 0, "y": 0},
                        userZoomingEnabled=False, userPanningEnabled=False,
                        autolock=True,
                    ),
                ], style=_CARD),
                html.Div([dcc.Graph(id="fig-queue", config={"displayModeBar": False})],
                         style=_CARD),
                html.Div([dcc.Graph(id="fig-latency", config={"displayModeBar": False})],
                         style=_CARD),
                html.Div([dcc.Graph(id="fig-qdepth", config={"displayModeBar": False})],
                         style=_CARD),
                html.Div([
                    html.Div("실시간 트레이스 로그 (gateway→bus→worker→store)",
                             style={"fontSize": "12px", "fontWeight": "bold"}),
                    html.Pre(id="trace-log", style={
                        "fontSize": "10px", "lineHeight": "1.3", "height": "150px",
                        "overflowY": "scroll", "backgroundColor": "#1e1e1e",
                        "color": "#d4d4d4", "padding": "6px", "margin": 0,
                        "borderRadius": "4px"}),
                ], style=_CARD),
            ], style={"width": "40%", "overflowY": "auto", "maxHeight": "660px"}),
        ], style={"display": "flex", "padding": "8px"}),
    ], style={"fontFamily": "system-ui, sans-serif", "backgroundColor": "#e8eaed",
              "minHeight": "100vh"})

    _register_callbacks(app, runner, config)
    return app


def _register_callbacks(app: dash.Dash, runner: SimRunner, config: SimConfig) -> None:

    @app.callback(
        Output("map", "figure"), Output("topology", "elements"),
        Output("fig-queue", "figure"), Output("fig-latency", "figure"),
        Output("fig-qdepth", "figure"), Output("trace-log", "children"),
        Output("b-time", "children"), Output("b-leader", "children"),
        Output("b-shard", "children"), Output("b-req", "children"),
        Output("b-rej", "children"), Output("b-p95", "children"),
        Output("b-recovery", "children"), Output("b-blocked", "children"),
        Output("b-msgrate", "children"), Output("worker-pick", "options"),
        Input("tick", "n_intervals"),
    )
    def _update(_n):
        snap = runner.snapshot()
        m = snap["metrics"]
        mapfig = build_map_figure(snap, config.area.center_lat, config.area.center_lon,
                                  config.area.map_style)
        recovery = f"{m['recovery_secs']:.0f}s" if m["recovery_secs"] is not None else "—"
        worker_opts = [{"label": n["id"], "value": n["id"]}
                       for n in snap["nodes"] if n["role"] == "worker" and n["alive"]]
        return (
            mapfig,
            build_topology_elements(snap),
            build_queue_figure(snap),
            build_latency_figure(snap),
            build_qdepth_timeline_figure(snap),
            "\n".join(snap["trace"]),
            f"{snap['sim_time']:.0f}",
            snap["leader"] or "—",
            f"v{snap['shard_version']}",
            f"{m['total_requests']} ({m['assigned']}/{m['completed']})",
            f"{m['rejected']}",
            f"{m['p95']:.2f}s",
            recovery,
            f"{m['blocked_conflicts'] + m['blocked_not_owner']}",
            f"{m['msg_rate']:.0f}",
            worker_opts,
        )

    @app.callback(Output("btn-play", "children"), Input("btn-play", "n_clicks"),
                  prevent_initial_call=True)
    def _toggle_play(n):
        playing = (n % 2) == 0
        runner.set_playing(playing)
        return "⏸ 일시정지" if playing else "▶ 재생"

    # 모든 컨트롤을 하나의 콜백으로 통합(triggered_id 로 분기) — 출력 중복 회피.
    @app.callback(
        Output("noop", "data"),
        Input("speed", "value"),
        Input("btn-kill-worker", "n_clicks"),
        Input("btn-kill-leader", "n_clicks"),
        Input("btn-surge", "n_clicks"),
        Input("btn-partition", "n_clicks"),
        Input("btn-heal", "n_clicks"),
        Input("btn-add", "n_clicks"),
        State("worker-pick", "value"),
        prevent_initial_call=True,
    )
    def _control(speed, _kw, _kl, _surge, _part, _heal, _add, worker_node):
        trig = dash.callback_context.triggered_id
        if trig == "speed":
            runner.set_speed(speed or 1.0)
        elif trig == "btn-kill-worker" and worker_node:
            runner.kill_worker(worker_node)
        elif trig == "btn-kill-leader":
            runner.kill_leader()
        elif trig == "btn-surge":
            runner.demand_surge(5.0)
        elif trig == "btn-partition":
            runner.partition(["worker-0"])
        elif trig == "btn-heal":
            runner.heal_partition()
        elif trig == "btn-add":
            runner.add_worker()
        return ""
