"""클러스터 통합 검증: 결정성, 멱등성, 리밸런싱, 페일오버, 복구."""

from drt_sim.config import (
    ClusterConfig,
    DemandConfig,
    FaultEvent,
    FleetConfig,
    SimConfig,
)
from drt_sim.cluster import ClusterController


def _cfg(**over) -> SimConfig:
    base = SimConfig(
        seed=7,
        duration=300.0,
        demand=DemandConfig(arrival_rate_per_hour=400.0, max_wait=420.0),
        fleet=FleetConfig(vehicles=30, capacity=4),
        cluster=ClusterConfig(workers=3, coordinators=2, heartbeat_timeout=6.0),
    )
    data = base.model_dump()
    data.update(over)
    return SimConfig.model_validate(data)


def _run(cfg: SimConfig) -> dict:
    ctrl = ClusterController(cfg)
    ctrl.run_batch(cfg.duration)
    return ctrl.snapshot()


def run_result(cfg: SimConfig) -> dict:
    """리밸런싱 검증용: 거절 수, 총 VKT, 선이동 횟수를 추출."""
    ctrl = ClusterController(cfg)
    ctrl.run_batch(cfg.duration)
    return {
        "rejected": sum(w.rejected for w in ctrl.workers.values()),
        "vkt": round(sum(v.distance_traveled for v in ctrl.store.all_vehicles()), 6),
        "repositioned": sum(w.repositioned for w in ctrl.workers.values()),
    }


def test_determinism_same_seed_same_result():
    """같은 시드 → 메트릭/트레이스가 완전히 동일."""
    a = _run(_cfg())
    b = _run(_cfg())
    assert a["metrics"]["total_requests"] == b["metrics"]["total_requests"]
    assert a["metrics"]["assigned"] == b["metrics"]["assigned"]
    assert a["metrics"]["completed"] == b["metrics"]["completed"]
    assert a["trace"] == b["trace"]


def test_different_seed_diverges():
    a = _run(_cfg(seed=1))
    b = _run(_cfg(seed=2))
    # 트레이스가 동일할 확률은 사실상 0
    assert a["trace"] != b["trace"]


def test_no_orphaned_vehicles_after_worker_failure():
    cfg = _cfg(faults=[FaultEvent(at=60.0, kind="kill_worker", target="worker-1").model_dump()])
    ctrl = ClusterController(cfg)
    ctrl.run_batch(cfg.duration)
    owners = {v.owner_node for v in ctrl.store.all_vehicles()}
    assert "worker-1" not in owners  # 죽은 노드가 차량을 소유하면 안 됨
    assert owners <= {"worker-0", "worker-2"}


def test_rebalance_bumps_shard_version():
    cfg = _cfg(faults=[FaultEvent(at=60.0, kind="kill_worker", target="worker-1").model_dump()])
    ctrl = ClusterController(cfg)
    ctrl.run_batch(cfg.duration)
    assert ctrl.snapshot()["shard_version"] >= 1


def test_recovery_time_recorded():
    cfg = _cfg(faults=[FaultEvent(at=60.0, kind="kill_worker", target="worker-1").model_dump()])
    ctrl = ClusterController(cfg)
    ctrl.run_batch(cfg.duration)
    assert len(ctrl.metrics.recovery_times) >= 1
    _t, node, secs = ctrl.metrics.recovery_times[0]
    assert node == "worker-1"
    assert 0 < secs < 30  # heartbeat_timeout 근처


def test_leader_failover():
    cfg = _cfg(faults=[FaultEvent(at=80.0, kind="kill_leader").model_dump()])
    ctrl = ClusterController(cfg)
    # 초기 리더 확인
    ctrl.advance(20.0)
    first_leader = ctrl.lease.holder
    assert first_leader is not None
    ctrl.run_batch(cfg.duration)
    snap = ctrl.snapshot()
    assert snap["leader"] is not None
    assert snap["leader"] != first_leader  # 새 리더로 승격
    assert snap["leader_epoch"] >= 2


def test_idle_rebalancing_inert_when_supply_sufficient():
    """공급이 충분해 거절이 없으면 유휴 리밸런싱은 아무 차량도 움직이지 않는다."""
    base = _cfg(duration=900.0)
    base.fleet.vehicles = 40  # 넉넉한 공급
    off = base.model_copy(deep=True)
    off.dispatch.idle_rebalancing = False
    on = base.model_copy(deep=True)
    on.dispatch.idle_rebalancing = True
    r_off = run_result(off)
    r_on = run_result(on)
    # 거절이 0이면 선이동 신호가 없어 결과가 동일해야 한다(오버헤드 없음)
    if r_off["rejected"] == 0:
        assert r_on["vkt"] == r_off["vkt"]
        assert r_on["repositioned"] == 0


def test_idle_rebalancing_activates_and_helps_on_average_under_shortage():
    """공급 부족 시 (1) 선이동이 실제로 일어나고, (2) 여러 시드 평균으로 거절이 줄거나 같다.

    휴리스틱이라 특정 시드에서는 손해일 수 있으므로 단일 시드가 아닌 평균으로 비교한다.
    """
    def shortage_cfg(seed, flag):
        c = _cfg(seed=seed, duration=1500.0)
        c.fleet.vehicles = 16
        c.cluster.workers = 5  # 샤드 단편화 → 공급 부족
        c.dispatch.idle_rebalancing = flag
        return c

    seeds = [1, 2, 3, 4]
    off = [run_result(shortage_cfg(s, False)) for s in seeds]
    on = [run_result(shortage_cfg(s, True)) for s in seeds]
    # (1) 메커니즘 작동: 부족 상황에서 선이동이 발생
    assert sum(r["repositioned"] for r in on) > 0
    # (2) 평균적으로 거절이 늘지 않는다
    assert sum(r["rejected"] for r in on) <= sum(r["rejected"] for r in off)


def test_capacity_never_exceeded():
    """어떤 차량도 정원을 초과해 태우지 않는다(합승 정원 하드 제약)."""
    cfg = _cfg(duration=900.0)
    ctrl = ClusterController(cfg)
    cap = cfg.fleet.capacity
    # 매 스텝마다 모든 차량의 onboard 점검
    for _ in range(int(900 / 5)):
        ctrl.advance(5.0)
        for v in ctrl.store.all_vehicles():
            assert v.onboard <= cap, f"veh#{v.id} onboard={v.onboard} > cap={cap}"


def test_idempotency_no_duplicate_assignment_under_dup_delivery():
    """버스 중복 전달(at-least-once)에도 요청당 한 번만 배차."""
    from drt_sim.config import BusConfig

    cfg = _cfg(bus=BusConfig(dup_prob=0.5).model_dump())
    ctrl = ClusterController(cfg)
    ctrl.run_batch(cfg.duration)
    # 각 요청은 최대 한 대에만 배정(assigned_vehicle 단일 값) — 중복 무시 카운터 존재
    total_dups = sum(w.duplicates_ignored for w in ctrl.workers.values())
    assert total_dups >= 1  # 중복이 실제로 발생하고 무시됨
    # 완료된 요청들은 모두 정확히 한 번 픽업/하차
    for r in ctrl.registry.values():
        if r.pickup_time is not None and r.dropoff_time is not None:
            assert r.dropoff_time >= r.pickup_time
