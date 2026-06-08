"""데모: 소규모 시나리오를 돌려 합승 배차 동작과 지표를 확인한다."""

from drt_sim.cli import build_vehicles
from drt_sim.demand import DemandGenerator
from drt_sim.engine import DispatchEngine
from drt_sim.simulator import Simulator


def main() -> None:
    area = 8.0
    gen = DemandGenerator(
        area_size_km=area,
        arrival_rate_per_hour=90.0,
        max_wait=300.0,
        max_detour_factor=1.6,
        seed=7,
    )
    requests = gen.generate(1800.0)  # 30분
    vehicles = build_vehicles(6, area, capacity=4)
    sim = Simulator(vehicles, requests, DispatchEngine(), time_step=10.0)

    print(f"생성된 요청: {len(requests)}건, 차량: {len(vehicles)}대\n")
    metrics = sim.run(1800.0)
    print(metrics.render())


if __name__ == "__main__":
    main()
