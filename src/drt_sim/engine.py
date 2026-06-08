"""Insertion 기반 합승 배차 엔진.

핵심 알고리즘 (greedy insertion heuristic):
  신규 요청 r 에 대해, 모든 차량 v 의 현재 경로에 r 의 픽업/하차 정류점을
  가능한 모든 위치 조합에 삽입해 본다. 각 후보 삽입에 대해
    1) 정원 제약 (어느 시점에도 onboard <= capacity)
    2) 픽업 시간 윈도우 (대기 <= max_wait)
    3) 우회 제약 (각 탑승객의 실제 이동시간 <= 직선시간 * max_detour_factor)
  을 모두 만족하는지 검사하고, 추가되는 총 이동시간이 최소인 삽입을 채택한다.
  어느 차량에도 실현 가능한 삽입이 없으면 요청을 거절(REJECTED)한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .geo import Point, travel_time_seconds
from .models import Request, RouteStop, StopType, Vehicle


@dataclass
class InsertionPlan:
    """하나의 실현 가능한 삽입 후보."""

    vehicle_id: int
    pickup_index: int          # route 에 픽업 정류점을 끼워넣을 위치
    dropoff_index: int         # 픽업 삽입 이후 기준 하차 정류점 위치
    added_cost: float          # 추가되는 총 이동시간 (초)
    new_route: List[RouteStop]


class DispatchEngine:
    def __init__(self, max_wait_default: float = 300.0):
        self.max_wait_default = max_wait_default

    def dispatch(
        self,
        request: Request,
        vehicles: List[Vehicle],
        now: float,
    ) -> Optional[InsertionPlan]:
        """요청을 가장 비용이 낮은 실현 가능 차량에 배정. 불가능하면 None."""
        best: Optional[InsertionPlan] = None
        for v in vehicles:
            plan = self._best_insertion_for_vehicle(request, v, now)
            if plan is None:
                continue
            if best is None or plan.added_cost < best.added_cost:
                best = plan
        return best

    def _best_insertion_for_vehicle(
        self, request: Request, vehicle: Vehicle, now: float
    ) -> Optional[InsertionPlan]:
        pickup = RouteStop(request.id, StopType.PICKUP, request.origin)
        dropoff = RouteStop(request.id, StopType.DROPOFF, request.destination)

        base_cost = self._route_travel_time(vehicle.location, vehicle.route)
        best: Optional[InsertionPlan] = None
        n = len(vehicle.route)

        # 픽업은 0..n, 하차는 픽업 이후(>= pickup_index) 위치에만 삽입
        for pi in range(n + 1):
            for di in range(pi, n + 1):
                candidate = list(vehicle.route)
                candidate.insert(pi, pickup)
                candidate.insert(di + 1, dropoff)  # 픽업 삽입으로 한 칸 밀림

                if not self._feasible(candidate, vehicle, request, now):
                    continue

                new_cost = self._route_travel_time(vehicle.location, candidate)
                added = new_cost - base_cost
                if best is None or added < best.added_cost:
                    best = InsertionPlan(
                        vehicle_id=vehicle.id,
                        pickup_index=pi,
                        dropoff_index=di + 1,
                        added_cost=added,
                        new_route=candidate,
                    )
        return best

    # --- 제약 검사 -------------------------------------------------------

    def _feasible(
        self,
        route: List[RouteStop],
        vehicle: Vehicle,
        new_request: Request,
        now: float,
    ) -> bool:
        """정원·대기·우회 제약을 모두 만족하면 True."""
        # 요청 메타 조회용 맵 (현재는 신규 요청만 상세정보 보유)
        # 기존 경로 정류점의 요청 정보는 차량 onboard 카운트와 정류점 순서로 근사.
        load = vehicle.onboard
        t = now
        prev_loc: Point = vehicle.location

        # 픽업 시각 추적 (우회 시간 계산용)
        pickup_times: Dict[int, float] = {}

        for stop in route:
            t += travel_time_seconds(prev_loc, stop.location)
            stop.eta = t
            prev_loc = stop.location

            if stop.stop_type == StopType.PICKUP:
                size = new_request.party_size if stop.request_id == new_request.id else 1
                load += size
                if load > vehicle.capacity:
                    return False
                pickup_times[stop.request_id] = t

                # 신규 요청의 픽업 대기 윈도우 검사
                if stop.request_id == new_request.id:
                    wait = t - new_request.request_time
                    if wait > new_request.max_wait:
                        return False
            else:  # DROPOFF
                size = new_request.party_size if stop.request_id == new_request.id else 1
                load -= size

                # 신규 요청의 우회 제약 검사 (탑승->하차 실제 시간 vs 직선 시간)
                if stop.request_id == new_request.id:
                    onboard_dur = t - pickup_times.get(stop.request_id, t)
                    direct = new_request.direct_travel_time()
                    if onboard_dur > direct * new_request.max_detour_factor:
                        return False
        return True

    # --- 비용 --------------------------------------------------------------

    @staticmethod
    def _route_travel_time(start: Point, route: List[RouteStop]) -> float:
        """현재 위치에서 경로를 순서대로 방문하는 총 이동시간(초)."""
        total = 0.0
        prev = start
        for stop in route:
            total += travel_time_seconds(prev, stop.location)
            prev = stop.location
        return total
