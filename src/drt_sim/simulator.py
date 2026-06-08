"""이산 시간(discrete-time) 시뮬레이션 루프.

고정 타임스텝마다:
  1) 이번 스텝에 도착한 신규 요청을 배차 엔진에 투입 (즉시 매칭)
  2) 각 차량을 경로를 따라 이동시키고, 정류점 도착 시 픽업/하차 이벤트 처리
  3) 지표 갱신
"""

from __future__ import annotations

from typing import List

from .engine import DispatchEngine
from .geo import Point, travel_time_seconds
from .metrics import Metrics
from .models import Request, RequestStatus, StopType, Vehicle


class Simulator:
    def __init__(
        self,
        vehicles: List[Vehicle],
        requests: List[Request],
        engine: DispatchEngine,
        time_step: float = 10.0,
    ):
        self.vehicles = vehicles
        # 도착 시각 순으로 정렬된 큐
        self.requests = sorted(requests, key=lambda r: r.request_time)
        self.engine = engine
        self.time_step = time_step
        self._by_id = {r.id: r for r in self.requests}
        self.now = 0.0

    def run(self, duration_seconds: float) -> Metrics:
        pending_idx = 0
        n = len(self.requests)

        while self.now <= duration_seconds:
            # 1) 이번 스텝까지 도착한 요청 배차
            while pending_idx < n and self.requests[pending_idx].request_time <= self.now:
                self._handle_request(self.requests[pending_idx])
                pending_idx += 1

            # 2) 차량 이동
            for v in self.vehicles:
                self._advance_vehicle(v, self.time_step)

            self.now += self.time_step

        return Metrics.from_state(self.requests, self.vehicles, duration_seconds)

    def _handle_request(self, request: Request) -> None:
        plan = self.engine.dispatch(request, self.vehicles, self.now)
        if plan is None:
            request.status = RequestStatus.REJECTED
            return
        vehicle = next(v for v in self.vehicles if v.id == plan.vehicle_id)
        vehicle.route = plan.new_route
        request.status = RequestStatus.ASSIGNED
        request.assigned_vehicle = vehicle.id

    def _advance_vehicle(self, vehicle: Vehicle, dt: float) -> None:
        """차량을 dt(초) 동안 경로를 따라 전진시키고 도착 이벤트를 처리."""
        if not vehicle.route:
            return

        remaining = dt
        while remaining > 0 and vehicle.route:
            stop = vehicle.route[0]
            tt = travel_time_seconds(vehicle.location, stop.location)
            if tt <= remaining:
                # 정류점 도착
                vehicle.distance_traveled += vehicle.location.distance_to(stop.location)
                vehicle.location = stop.location
                vehicle.busy_time += tt
                remaining -= tt
                self._process_stop(vehicle, stop)
                vehicle.route.pop(0)
            else:
                # 정류점 방향으로 부분 이동 (선형 보간)
                frac = remaining / tt
                nx = vehicle.location.x + (stop.location.x - vehicle.location.x) * frac
                ny = vehicle.location.y + (stop.location.y - vehicle.location.y) * frac
                new_loc = Point(nx, ny)
                vehicle.distance_traveled += vehicle.location.distance_to(new_loc)
                vehicle.busy_time += remaining
                vehicle.location = new_loc
                remaining = 0.0

    def _process_stop(self, vehicle: Vehicle, stop) -> None:
        req = self._by_id.get(stop.request_id)
        if req is None:
            return
        arrival = self.now + (self.time_step)  # 근사: 스텝 내 도착
        if stop.stop_type == StopType.PICKUP:
            vehicle.onboard += req.party_size
            req.status = RequestStatus.ONBOARD
            req.pickup_time = self.now
        else:
            vehicle.onboard = max(0, vehicle.onboard - req.party_size)
            req.status = RequestStatus.COMPLETED
            req.dropoff_time = self.now
