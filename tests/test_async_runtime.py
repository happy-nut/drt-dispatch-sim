"""Wall-clock asyncio 런타임 + live 클러스터 검증.

같은 actor 코드가 가상시간 런타임뿐 아니라 진짜 asyncio 루프에서도 동작함을 보인다.
"""

import asyncio

from drt_sim.async_runtime import AsyncioRuntime
from drt_sim.config import ClusterConfig, DemandConfig, FleetConfig, SimConfig


def test_asyncio_runtime_ping_pong():
    """AsyncioRuntime 위에서 actor 가 sleep/recv 로 동작하고 deliver 로 깨어난다."""
    rt = AsyncioRuntime(time_scale=50.0)
    got = []

    class Pinger:
        async def run(self, ctx):
            for i in range(3):
                # 직접 상대 인박스로 전달(버스 없이 런타임 deliver 사용)
                rt.deliver("pong", i)
                await ctx.sleep(1.0)

    class Ponger:
        async def run(self, ctx):
            while True:
                msg = await ctx.recv()
                got.append(msg)

    rt.spawn("pong", Ponger().run)
    rt.spawn("ping", Pinger().run)
    asyncio.run(rt.run_for(0.2))  # 가상 ~10s @ 50x
    assert got == [0, 1, 2]


def test_live_cluster_runs_on_real_asyncio():
    """live 모드: 전체 클러스터가 진짜 asyncio 에서 배차를 수행하고 정원을 지킨다."""
    from drt_sim.live_cluster import run_live

    cfg = SimConfig(
        seed=3, duration=120.0,
        demand=DemandConfig(arrival_rate_per_hour=900.0, max_wait=420.0),
        fleet=FleetConfig(vehicles=24, capacity=4),
        cluster=ClusterConfig(workers=3, coordinators=2),
    )
    ctrl = run_live(cfg, virtual_seconds=40.0, time_scale=40.0)  # ~1s wall
    reqs = list(ctrl.registry.values())
    assert ctrl.runtime.now >= 30
    assert len(reqs) > 0
    assert any(r.assigned_vehicle is not None for r in reqs)
    assert ctrl.lease.holder is not None              # 리더 선출됨
    for v in ctrl.store.all_vehicles():
        assert v.onboard <= cfg.fleet.capacity        # 정원 불변
    assert ctrl.store.blocked_conflicts == 0          # 이중 배차 없음
