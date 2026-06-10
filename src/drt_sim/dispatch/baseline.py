"""베이스라인: 비합승(가장 가까운 유휴 차량) 매칭.

합승 엔진과의 비교용. 각 요청을 **현재 유휴(경로 없음)인 가장 가까운 차량**에 단독
배정한다. 합승이 없으므로 차량 한 대가 한 번에 한 요청만 처리 → 같은 차량 수로 더 적게
태우고 VKT(총주행거리)가 늘어남을 보여주는 대조군이다.
"""

from __future__ import annotations

from typing import List, Optional

from ..models import Request, RouteStop, StopType, Vehicle
from .insertion import InsertionPlan


class NearestIdleEngine:
    """비합승 최근접 유휴 차량 배차."""

    def dispatch(
        self, request: Request, vehicles: List[Vehicle], now: float
    ) -> Optional[InsertionPlan]:
        best: Optional[Vehicle] = None
        best_d = float("inf")
        for v in vehicles:
            if not v.is_idle():
                continue  # 합승 안 함 — 유휴 차량만
            d = v.location.distance_to(request.origin)
            if d < best_d:
                best_d = d
                best = v
        if best is None:
            return None
        meta = dict(
            request_time=request.request_time,
            max_wait=request.max_wait,
            direct_time=request.direct_travel_time(),
            max_detour=request.max_detour_factor,
        )
        route = [
            RouteStop(request.id, StopType.PICKUP, request.origin, request.party_size, **meta),
            RouteStop(request.id, StopType.DROPOFF, request.destination, request.party_size, **meta),
        ]
        return InsertionPlan(
            vehicle_id=best.id,
            pickup_index=0,
            dropoff_index=1,
            added_cost=best_d,
            new_route=route,
            expected_version=best.version,
        )
