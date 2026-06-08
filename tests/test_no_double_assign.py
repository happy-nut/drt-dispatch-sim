"""이중 배차 방지(no-double-assignment) 검증.

단일 라이터 + 낙관적 동시성이 두 워커의 경쟁 쓰기를 어떻게 차단하는지 증명한다.
"""

from drt_sim.geo import Point
from drt_sim.models import RouteStop, StopType, Vehicle
from drt_sim.store import VehicleStore


def _veh(owner="worker-0"):
    v = Vehicle(id=1, location=Point(0, 0), capacity=4, home_cell="c", owner_node=owner)
    return v


def _route():
    return [
        RouteStop(10, StopType.PICKUP, Point(1, 1)),
        RouteStop(10, StopType.DROPOFF, Point(2, 2)),
    ]


def test_owner_commit_succeeds_and_bumps_version():
    store = VehicleStore()
    store.register(_veh())
    res = store.commit_assignment(1, expected_version=0, new_route=_route(), by_node="worker-0")
    assert res.ok
    assert res.version == 1
    assert store.get(1).version == 1


def test_non_owner_commit_blocked():
    store = VehicleStore()
    store.register(_veh(owner="worker-0"))
    # worker-1 이 남의 차량을 건드리려 함 → 차단
    res = store.commit_assignment(1, expected_version=0, new_route=_route(), by_node="worker-1")
    assert not res.ok
    assert res.reason == "not_owner"
    assert store.blocked_not_owner == 1
    assert store.get(1).route == []  # 변경 없음


def test_concurrent_writers_only_one_wins():
    """같은 차량을 같은 버전 기준으로 두 번 커밋 → 둘째는 version_conflict."""
    store = VehicleStore()
    store.register(_veh(owner="worker-0"))
    # 둘 다 버전 0 을 읽고 커밋 시도(경쟁)
    first = store.commit_assignment(1, 0, _route(), by_node="worker-0")
    second = store.commit_assignment(1, 0, _route(), by_node="worker-0")
    assert first.ok
    assert not second.ok
    assert second.reason == "version_conflict"
    assert store.blocked_conflicts == 1


def test_reassign_invalidates_stale_writes():
    """소유권 이전(리밸런싱) 후, 옛 소유자의 스테일 커밋은 무효."""
    store = VehicleStore()
    store.register(_veh(owner="worker-0"))
    # 옛 소유자가 버전 0 을 읽음(아직 커밋 전)
    stale_version = store.get(1).version
    # 리밸런싱: 소유권이 worker-2 로 이전(버전 증가)
    store.reassign_owner(1, "worker-2")
    # 옛 소유자가 뒤늦게 커밋 시도 → not_owner 로 차단
    res = store.commit_assignment(1, stale_version, _route(), by_node="worker-0")
    assert not res.ok
    assert res.reason == "not_owner"
