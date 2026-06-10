"""World / 차량 물리 모델 (데이터 플레인).

제어 플레인(워커·코디네이터)과 분리된 **물리 세계**다. 매 ``motion_dt`` 가상초마다
스토어에 커밋된 경로를 따라 모든 차량을 이동시키고, 정류점 도착 시 픽업/하차 이벤트를
처리하며 요청 상태/타임스탬프를 갱신한다.

도로 형상 따라가기
------------------
두 정류점 사이를 직선이 아니라 **도로 폴리라인(:class:`Router`)을 따라** 이동시킨다.
단, 도착 시각은 엔진이 가정한 **유클리드 통행시간**에 맞춘다(폴리라인 호 길이 기준
보간). 따라서 일정·거리 메트릭·결정론은 직선 모드와 동일하고, 화면상 경로 형상만
도로를 따른다. ``StraightRouter`` 면 직선 모드와 바이트 단위로 동일하다.

배차(경로 삽입)는 워커가 ``commit_assignment`` 로만 하고, 여기서는 경로 진행과 위치
갱신만 한다. 한 이벤트는 원자적으로 실행되므로 워커의 읽기-커밋과 섞이지 않는다.
"""

from __future__ import annotations

import bisect
from typing import Dict, Optional, Tuple

from .bus import VEHICLE_TELEMETRY, Message, SimBus
from .geo import Point, travel_time_seconds
from .models import Request, RequestStatus, StopType, Vehicle
from .routing import Router, StraightRouter
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
        router: Optional[Router] = None,
    ) -> None:
        self.bus = bus
        self.store = store
        self.requests = requests
        self.tracer = tracer
        self.motion_dt = motion_dt
        self.router: Router = router or StraightRouter()
        # 차량별 현재 이동 구간(도로 폴리라인 + 진행 상태)
        self._seg: Dict[int, dict] = {}

    async def run(self, ctx: ActorContext) -> None:
        while True:
            for v in self.store.all_vehicles():
                self._advance(v, self.motion_dt, ctx)
            self.bus.publish(Message(
                VEHICLE_TELEMETRY,
                {"sim_time": ctx.now, "vehicles": len(self.store.all_vehicles())},
                sender="world",
            ))
            await ctx.sleep(self.motion_dt)

    # --- 이동 -----------------------------------------------------------

    def _current_target(self, vehicle: Vehicle) -> Tuple[Optional[Point], bool]:
        """현재 이동 목표와 (정류점 여부). 없으면 (None, False)."""
        if vehicle.route:
            return vehicle.route[0].location, True
        if vehicle.reposition_target is not None:
            return vehicle.reposition_target, False
        return None, False

    def _segment(self, vehicle: Vehicle, target: Point) -> dict:
        """현재 위치→target 도로 폴리라인 구간을 (캐시) 만든다."""
        seg = self._seg.get(vehicle.id)
        if seg is not None and seg["to"] == target:
            return seg
        start = vehicle.location
        poly = self.router.path_km(start, target)
        cum = [0.0]
        for i in range(1, len(poly)):
            cum.append(cum[-1] + poly[i - 1].distance_to(poly[i]))
        seg = {
            "to": target,
            "poly": poly,
            "cum": cum,
            "total_road": cum[-1],
            "straight_dist": start.distance_to(target),
            "straight_tt": travel_time_seconds(start, target),
            "elapsed": 0.0,
        }
        self._seg[vehicle.id] = seg
        return seg

    def _interp(self, seg: dict, f: float) -> Point:
        poly = seg["poly"]
        if seg["total_road"] <= 1e-12 or f >= 1.0:
            return poly[-1]
        if f <= 0.0:
            return poly[0]
        d = f * seg["total_road"]
        cum = seg["cum"]
        i = max(0, min(bisect.bisect_right(cum, d) - 1, len(poly) - 2))
        seg_len = cum[i + 1] - cum[i]
        if seg_len <= 1e-12:
            return poly[i]
        t = (d - cum[i]) / seg_len
        a, b = poly[i], poly[i + 1]
        return Point(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t)

    def _advance(self, vehicle: Vehicle, dt: float, ctx: ActorContext) -> None:
        remaining = dt
        while remaining > 1e-12:
            target, is_stop = self._current_target(vehicle)
            if target is None:
                self._seg.pop(vehicle.id, None)
                return
            seg = self._segment(vehicle, target)
            tt = seg["straight_tt"]
            if tt <= 1e-9:
                arrived, used = True, 0.0
            else:
                avail = tt - seg["elapsed"]
                if remaining >= avail:
                    arrived, used = True, avail
                else:
                    arrived, used = False, remaining
            seg["elapsed"] += used
            remaining -= used
            if tt > 1e-9:
                vehicle.distance_traveled += seg["straight_dist"] * (used / tt)
                vehicle.busy_time += used
                vehicle.location = self._interp(seg, seg["elapsed"] / tt)
            if arrived:
                vehicle.location = target
                self._seg.pop(vehicle.id, None)
                if is_stop:
                    stop = vehicle.route[0]
                    self._process_stop(vehicle, stop, ctx)
                    vehicle.route.pop(0)
                else:
                    vehicle.reposition_target = None
            else:
                break

    def _process_stop(self, vehicle: Vehicle, stop, ctx: ActorContext) -> None:
        req = self.requests.get(stop.request_id)
        if req is None:
            return
        if stop.stop_type == StopType.PICKUP:
            vehicle.onboard += req.party_size
            req.status = RequestStatus.ONBOARD
            req.pickup_time = ctx.now
            # 남은 하차 정류점에 실제 탑승 시각을 기록 → 이후 삽입 시 우회 재검증 기준.
            for s in vehicle.route:
                if s.request_id == req.id and s.stop_type == StopType.DROPOFF:
                    s.boarded_at = ctx.now
                    break
            self.tracer.emit(ctx.now, req.trace_id, "world", "pickup",
                             f"veh#{vehicle.id}", req.id)
        else:
            vehicle.onboard = max(0, vehicle.onboard - req.party_size)
            req.status = RequestStatus.COMPLETED
            req.dropoff_time = ctx.now
            self.tracer.emit(ctx.now, req.trace_id, "world", "dropoff",
                             f"veh#{vehicle.id}", req.id)
