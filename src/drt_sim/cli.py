"""CLI 진입점.

서브커맨드
----------
- ``dashboard`` : 분산 클러스터 + Plotly Dash 대시보드 기동(단일 커맨드 데모).
- ``run``       : 배치 실행 후 서비스+분산 메트릭 출력(시나리오/시드 지정).
- ``baseline``  : 합승 vs 비합승 비교.
- ``scaling``   : 워커 수를 늘리며 확장성 측정.

예)
    python -m drt_sim.cli dashboard --config config/default.yaml
    python -m drt_sim.cli run --scenario scenarios/node_failure.yaml
    python -m drt_sim.cli baseline --config config/default.yaml
    python -m drt_sim.cli scaling --config config/default.yaml --workers 1,2,4,6
"""

from __future__ import annotations

import argparse
from typing import List, Optional

from .config import SimConfig


def _load(args) -> SimConfig:
    path = getattr(args, "config", None) or getattr(args, "scenario", None)
    cfg = SimConfig.from_yaml_or_default(path)
    if getattr(args, "seed", None) is not None:
        cfg.seed = args.seed
    if getattr(args, "duration", None) is not None:
        cfg.duration = args.duration
    return cfg


def cmd_dashboard(args) -> int:
    from .viz import create_app

    cfg = _load(args)
    app = create_app(cfg)
    print(f"▶ 대시보드: http://{args.host}:{args.port}  (시드 {cfg.seed}, "
          f"워커 {cfg.cluster.workers}, 차량 {cfg.fleet.vehicles})")
    app.run(host=args.host, port=args.port, debug=False)
    return 0


def cmd_run(args) -> int:
    from .experiments import run_once

    cfg = _load(args)
    print(f"시나리오: 워커 {cfg.cluster.workers} | 차량 {cfg.fleet.vehicles} | "
          f"{cfg.duration:.0f}s | 시드 {cfg.seed} | 장애 {len(cfg.faults)}건\n")
    res = run_once(cfg, args.label or "run")
    print(res.render())
    return 0


def cmd_baseline(args) -> int:
    from .experiments import compare_baseline

    cfg = _load(args)
    print("=== 합승 vs 비합승 비교 (동일 수요·차량) ===\n")
    for res in compare_baseline(cfg):
        print(res.render())
        print()
    return 0


def cmd_cluster(args) -> int:
    if args.mode == "redis":
        from .cluster_redis import launch_redis_cluster

        launch_redis_cluster(
            workers=args.workers, coordinators=args.coordinators,
            duration=args.cluster_duration, kill_worker_at=args.kill_at,
            redis_url=args.redis_url,
        )
        return 0
    # live(asyncio) 모드 — redis 불필요, 어디서나 실행 가능
    from .live_cluster import run_live, summarize

    cfg = _load(args)
    cfg.cluster.workers = args.workers
    cfg.cluster.coordinators = args.coordinators
    print(f"▶ live(asyncio) 클러스터 — 같은 actor 가 진짜 asyncio 루프에서 실행 "
          f"(배속 {args.scale}x, 가상 {args.virtual_seconds:.0f}s)\n")
    ctrl = run_live(cfg, virtual_seconds=args.virtual_seconds, time_scale=args.scale)
    print(summarize(ctrl))
    return 0


def cmd_scaling(args) -> int:
    from .experiments import scaling_experiment

    cfg = _load(args)
    counts: List[int] = [int(x) for x in args.workers.split(",")]
    print(f"=== 확장성 실험: 워커 {counts} (고정 부하) ===\n")
    for res in scaling_experiment(cfg, counts):
        print(res.render())
        print()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="DRT 분산 합승 배차 엔진 시뮬레이터")
    sub = p.add_subparsers(dest="cmd")

    def add_common(sp):
        sp.add_argument("--config", type=str, default=None, help="YAML 설정 파일")
        sp.add_argument("--scenario", type=str, default=None, help="YAML 시나리오 파일")
        sp.add_argument("--seed", type=int, default=None, help="난수 시드(설정 덮어쓰기)")
        sp.add_argument("--duration", type=float, default=None, help="시뮬레이션 시간(초)")

    sp = sub.add_parser("dashboard", help="클러스터 + Dash 대시보드 기동")
    add_common(sp)
    sp.add_argument("--host", type=str, default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8050)
    sp.set_defaults(func=cmd_dashboard)

    sp = sub.add_parser("run", help="배치 실행 + 메트릭 출력")
    add_common(sp)
    sp.add_argument("--label", type=str, default=None)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("baseline", help="합승 vs 비합승 비교")
    add_common(sp)
    sp.set_defaults(func=cmd_baseline)

    sp = sub.add_parser("scaling", help="워커 수 확장성 실험")
    add_common(sp)
    sp.add_argument("--workers", type=str, default="1,2,4,6")
    sp.set_defaults(func=cmd_scaling)

    sp = sub.add_parser("cluster", help="진짜 분산: live(asyncio) 또는 redis(멀티프로세스)")
    add_common(sp)
    sp.add_argument("--mode", choices=["live", "redis"], default="live")
    sp.add_argument("--workers", type=int, default=3)
    sp.add_argument("--coordinators", type=int, default=2)
    sp.add_argument("--scale", type=float, default=10.0, help="live 배속")
    sp.add_argument("--virtual-seconds", type=float, default=30.0, help="live 실행 가상시간")
    sp.add_argument("--cluster-duration", type=float, default=8.0, help="redis 모드 실행 초")
    sp.add_argument("--kill-at", type=float, default=4.0, help="redis 모드: 워커 종료 시각(초)")
    sp.add_argument("--redis-url", type=str, default="redis://localhost:6379/0")
    sp.set_defaults(func=cmd_cluster)

    args = p.parse_args(argv)
    if not getattr(args, "cmd", None):
        # 기본: 대시보드
        return cmd_dashboard(p.parse_args(["dashboard"]))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
