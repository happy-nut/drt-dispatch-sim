"""World / 차량 물리 모델 (데이터 플레인).

제어 플레인(워커·코디네이터)과 분리된 **물리 세계**다. 매 ``motion_dt`` 가상초마다
스토어에 커밋된 경로를 따라 모든 차량을 이동시키고, 정류점 도착 시 픽업/하차 이벤트를
처리하며 요청 상태/타임스탬프를 갱신한다. 결정론적 물리를 한곳에 모아 둔다.

배차(경로 삽입)는 워커가 ``commit_assignment`` 로만 하고, 여기서는 경로 진행(정류점
소비)과 위치 갱신만 한다. 한 이벤트는 원자적으로 실행되므로 워커의 읽기-커밋과
World 의 경로 소비가 섞이지 않는다.
"""

from __future__ import annotations

from typing import Dict

from .bus import VEHICLE_TELEMETRY, Message, SimBus
from .geo import Point, travel_time_seconds
from .models import Request, RequestStatus, StopType, Vehicle
from .sim_clock import ActorContext
from .store import VehicleStore
from .tracing import Tracer


class World:
    def __init__(
        self,
        bus: SimBus,
        store: VehicleStore,
        requests: Dict[int, Request],
        tracer: Tracer,
        *,
        motion_dt: float = 1.0,
    ) -> None:
        self.bus = bus
        self.store = store
        self.requests = requests
        self.tracer = tracer
        self.motion_dt = motion_dt

    async def run(self, ctx: ActorContext) -> None:
        while True:
            for v in self.store.all_vehicles():
                self._advance(v, self.motion_dt, ctx)
            # 토픽 유지를 위한 경량 집계 텔레메트리(메시지 스트림/메트릭 가시화).
            self.bus.publish(Message(
                VEHICLE_TELEMETRY,
                {"sim_time": ctx.now, "vehicles": len(self.store.all_vehicles())},
                sender="world",
            ))
            await ctx.sleep(self.motion_dt)

    def _advance(self, vehicle: Vehicle, dt: float, ctx: ActorContext) -> None:
        if not vehicle.route:
            self._advance_reposition(vehicle, dt)
            return
        remaining = dt
        while remaining > 0 and vehicle.route:
            stop = vehicle.route[0]
            tt = travel_time_seconds(vehicle.location, stop.location)
            if tt <= remaining:
                vehicle.distance_traveled += vehicle.location.distance_to(stop.location)
                vehicle.location = stop.location
                vehicle.busy_time += tt
                remaining -= tt
                self._process_stop(vehicle, stop, ctx)
                vehicle.route.pop(0)
            else:
                frac = remaining / tt
                nx = vehicle.location.x + (stop.location.x - vehicle.location.x) * frac
                ny = vehicle.location.y + (stop.location.y - vehicle.location.y) * frac
                new_loc = Point(nx, ny)
                vehicle.distance_traveled += vehicle.location.distance_to(new_loc)
                vehicle.busy_time += remaining
                vehicle.location = new_loc
                remaining = 0.0

    def _advance_reposition(self, vehicle: Vehicle, dt: float) -> None:
        """승객 없는 유휴 리밸런싱 이동(deadhead). 목적지 도달 시 해제."""
        tgt = vehicle.reposition_target
        if tgt is None:
            return
        dist = vehicle.location.distance_to(tgt)
        if dist < 1e-6:
            vehicle.reposition_target = None
            return
        reach = travel_time_seconds(vehicle.location, tgt)
        if reach <= dt:
            vehicle.distance_traveled += dist
            vehicle.location = tgt
            vehicle.reposition_target = None
        else:
            frac = dt / reach
            nx = vehicle.location.x + (tgt.x - vehicle.location.x) * frac
            ny = vehicle.location.y + (tgt.y - vehicle.location.y) * frac
            new_loc = Point(nx, ny)
            vehicle.distance_traveled += vehicle.location.distance_to(new_loc)
            vehicle.location = new_loc

    def _process_stop(self, vehicle: Vehicle, stop, ctx: ActorContext) -> None:
        req = self.requests.get(stop.request_id)
        if req is None:
            return
        if stop.stop_type == StopType.PICKUP:
            vehicle.onboard += req.party_size
            req.status = RequestStatus.ONBOARD
            req.pickup_time = ctx.now
            self.tracer.emit(ctx.now, req.trace_id, "world", "pickup",
                             f"veh#{vehicle.id}", req.id)
        else:
            vehicle.onboard = max(0, vehicle.onboard - req.party_size)
            req.status = RequestStatus.COMPLETED
            req.dropoff_time = ctx.now
            self.tracer.emit(ctx.now, req.trace_id, "world", "dropoff",
                             f"veh#{vehicle.id}", req.id)
