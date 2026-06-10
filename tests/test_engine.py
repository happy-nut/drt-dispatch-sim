"""배차 엔진의 핵심 동작과 제약 검사를 검증."""

from drt_sim.engine import DispatchEngine
from drt_sim.geo import Point, travel_time_seconds
from drt_sim.models import Request, RouteStop, StopType, Vehicle


def _meta(direct_from, direct_to, max_detour=1.2, max_wait=1e9):
    return dict(request_time=0.0, max_wait=max_wait,
               direct_time=travel_time_seconds(direct_from, direct_to),
               max_detour=max_detour)


def make_request(rid, ox, oy, dx, dy, t=0.0, **kw):
    return Request(
        id=rid,
        origin=Point(ox, oy),
        destination=Point(dx, dy),
        request_time=t,
        **kw,
    )


def test_idle_vehicle_gets_assigned():
    engine = DispatchEngine()
    v = Vehicle(id=0, location=Point(0, 0), capacity=4)
    r = make_request(1, 0, 0, 2, 0)
    plan = engine.dispatch(r, [v], now=0.0)
    assert plan is not None
    assert plan.vehicle_id == 0
    # 픽업 -> 하차 순서 보장
    types = [s.stop_type for s in plan.new_route]
    assert types == [StopType.PICKUP, StopType.DROPOFF]


def test_pickup_before_dropoff_in_route():
    engine = DispatchEngine()
    v = Vehicle(id=0, location=Point(0, 0))
    r = make_request(1, 1, 1, 5, 5)
    plan = engine.dispatch(r, [v], now=0.0)
    assert plan is not None
    pickup_idx = next(i for i, s in enumerate(plan.new_route)
                      if s.stop_type == StopType.PICKUP)
    dropoff_idx = next(i for i, s in enumerate(plan.new_route)
                       if s.stop_type == StopType.DROPOFF)
    assert pickup_idx < dropoff_idx


def test_wait_window_rejects_far_request():
    engine = DispatchEngine()
    # 차량이 아주 멀리 있어 max_wait 안에 도달 불가
    v = Vehicle(id=0, location=Point(100, 100))
    r = make_request(1, 0, 0, 1, 0, max_wait=60.0)
    plan = engine.dispatch(r, [v], now=0.0)
    assert plan is None


def test_capacity_constraint():
    engine = DispatchEngine()
    v = Vehicle(id=0, location=Point(0, 0), capacity=2, onboard=2)
    r = make_request(1, 0, 0, 1, 0, party_size=1)
    plan = engine.dispatch(r, [v], now=0.0)
    # 이미 정원 가득 -> 추가 픽업 불가
    assert plan is None


def test_picks_lower_cost_vehicle():
    engine = DispatchEngine()
    near = Vehicle(id=0, location=Point(0, 0))
    far = Vehicle(id=1, location=Point(5, 5))
    r = make_request(1, 0, 0, 1, 0)
    plan = engine.dispatch(r, [near, far], now=0.0)
    assert plan is not None
    assert plan.vehicle_id == 0


def test_existing_passenger_protected_from_overdetour():
    """삽입이 이미 배차된 다른 승객을 한도 넘게 돌리면 그 경로는 불가능 판정."""
    engine = DispatchEngine()
    v = Vehicle(id=0, location=Point(0, 0), capacity=4)
    e = _meta(Point(0, 0), Point(0, 1), max_detour=1.2)   # E: (0,0)->(0,1) 직선, 우회 1.2배
    n = _meta(Point(0, 0), Point(10, 0), max_detour=5.0)  # N: (0,0)->(10,0)
    # E 사이에 N 을 끼워 E 가 동쪽으로 크게 돌게 만든 경로 → E 우회 위반
    bad = [
        RouteStop(1, StopType.PICKUP, Point(0, 0), 1, **e),
        RouteStop(2, StopType.PICKUP, Point(0, 0), 1, **n),
        RouteStop(2, StopType.DROPOFF, Point(10, 0), 1, **n),
        RouteStop(1, StopType.DROPOFF, Point(0, 1), 1, **e),
    ]
    assert engine._feasible(bad, v, now=0.0) is False
    # E 를 먼저 내려주는 순서면 E 직선 → 가능
    ok = [
        RouteStop(1, StopType.PICKUP, Point(0, 0), 1, **e),
        RouteStop(1, StopType.DROPOFF, Point(0, 1), 1, **e),
        RouteStop(2, StopType.PICKUP, Point(0, 0), 1, **n),
        RouteStop(2, StopType.DROPOFF, Point(10, 0), 1, **n),
    ]
    assert engine._feasible(ok, v, now=0.0) is True


def test_onboard_passenger_detour_checked_via_boarded_at():
    """이미 탑승한 승객(boarded_at)도 우회 한도를 재검증한다."""
    engine = DispatchEngine()
    v = Vehicle(id=0, location=Point(0, 0), capacity=4, onboard=1)
    e = _meta(Point(0, 0), Point(0, 1), max_detour=1.2)
    # 오래 전(0초)에 탑승했고 now=10000 → 차내시간이 직선×1.2 를 한참 초과
    route = [RouteStop(1, StopType.DROPOFF, Point(0, 1), 1, boarded_at=0.0, **e)]
    assert engine._feasible(route, v, now=10000.0) is False


def test_travel_time_positive():
    a, b = Point(0, 0), Point(3, 4)  # 거리 5km
    tt = travel_time_seconds(a, b, speed_kmh=18.0)
    assert abs(tt - (5 / 18 * 3600)) < 1e-6
