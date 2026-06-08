"""시나리오 실행 진입점.

예: python -m drt_sim.cli --vehicles 10 --duration 3600 --rate 120 --seed 42
"""

from __future__ import annotations

import argparse

from .demand import DemandGenerator
from .engine import DispatchEngine
from .geo import Point
from .models import Vehicle
from .simulator import Simulator


def build_vehicles(count: int, area_km: float, capacity: int) -> list[Vehicle]:
    """차량을 서비스 구역에 격자 형태로 균등 배치."""
    vehicles = []
    # 정사각 격자에 가깝게 배치
    cols = max(1, int(count ** 0.5))
    for i in range(count):
        r, c = divmod(i, cols)
        x = (c + 0.5) / cols * area_km
        y = (r + 0.5) / cols * area_km
        vehicles.append(Vehicle(id=i, location=Point(x, y), capacity=capacity))
    return vehicles


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="DRT 합승 배차 엔진 시뮬레이터")
    p.add_argument("--vehicles", type=int, default=10, help="차량 수")
    p.add_argument("--capacity", type=int, default=4, help="차량 정원")
    p.add_argument("--duration", type=float, default=3600.0, help="시뮬레이션 시간(초)")
    p.add_argument("--rate", type=float, default=120.0, help="시간당 호출 수")
    p.add_argument("--area", type=float, default=10.0, help="서비스 구역 한 변(km)")
    p.add_argument("--max-wait", type=float, default=300.0, help="최대 대기(초)")
    p.add_argument("--detour", type=float, default=1.5, help="허용 우회 배율")
    p.add_argument("--step", type=float, default=10.0, help="타임스텝(초)")
    p.add_argument("--seed", type=int, default=42, help="난수 시드")
    args = p.parse_args(argv)

    gen = DemandGenerator(
        area_size_km=args.area,
        arrival_rate_per_hour=args.rate,
        max_wait=args.max_wait,
        max_detour_factor=args.detour,
        seed=args.seed,
    )
    requests = gen.generate(args.duration)
    vehicles = build_vehicles(args.vehicles, args.area, args.capacity)
    engine = DispatchEngine(max_wait_default=args.max_wait)
    sim = Simulator(vehicles, requests, engine, time_step=args.step)

    print(
        f"시나리오: 차량 {args.vehicles}대(정원 {args.capacity}) | "
        f"{args.duration:.0f}초 | 호출 {args.rate}/h | 구역 {args.area}km | "
        f"생성된 요청 {len(requests)}건\n"
    )
    metrics = sim.run(args.duration)
    print(metrics.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
