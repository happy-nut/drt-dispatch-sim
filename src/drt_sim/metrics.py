"""시뮬레이션 결과 지표 집계."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .models import Request, RequestStatus, Vehicle


@dataclass
class Metrics:
    total_requests: int
    completed: int
    assigned_or_better: int
    rejected: int
    match_rate: float            # (배차 성공) / 전체
    completion_rate: float       # (하차 완료) / 전체
    avg_wait_seconds: float      # 호출 -> 픽업 평균
    avg_onboard_seconds: float   # 픽업 -> 하차 평균 (실제 차내 시간)
    avg_detour_ratio: float      # 실제 차내시간 / 직선 이동시간 평균
    vehicle_utilization: float   # busy_time / (차량수 * 시뮬레이션시간)
    total_distance_km: float

    @classmethod
    def from_state(
        cls,
        requests: List[Request],
        vehicles: List[Vehicle],
        duration_seconds: float,
    ) -> "Metrics":
        total = len(requests)
        completed = [r for r in requests if r.status == RequestStatus.COMPLETED]
        rejected = [r for r in requests if r.status == RequestStatus.REJECTED]
        matched = [r for r in requests if r.status != RequestStatus.REJECTED
                   and r.status != RequestStatus.PENDING]

        waits = [
            r.pickup_time - r.request_time
            for r in requests
            if r.pickup_time is not None
        ]
        onboards = [
            r.dropoff_time - r.pickup_time
            for r in completed
            if r.dropoff_time is not None and r.pickup_time is not None
        ]
        detours = [
            (r.dropoff_time - r.pickup_time) / r.direct_travel_time()
            for r in completed
            if r.dropoff_time is not None
            and r.pickup_time is not None
            and r.direct_travel_time() > 0
        ]

        busy = sum(v.busy_time for v in vehicles)
        denom = len(vehicles) * duration_seconds if vehicles and duration_seconds else 1.0

        def _avg(xs: List[float]) -> float:
            return sum(xs) / len(xs) if xs else 0.0

        return cls(
            total_requests=total,
            completed=len(completed),
            assigned_or_better=len(matched),
            rejected=len(rejected),
            match_rate=len(matched) / total if total else 0.0,
            completion_rate=len(completed) / total if total else 0.0,
            avg_wait_seconds=_avg(waits),
            avg_onboard_seconds=_avg(onboards),
            avg_detour_ratio=_avg(detours),
            vehicle_utilization=busy / denom,
            total_distance_km=sum(v.distance_traveled for v in vehicles),
        )

    def render(self) -> str:
        return (
            "=== 시뮬레이션 결과 ===\n"
            f"총 요청        : {self.total_requests}\n"
            f"배차 성공      : {self.assigned_or_better} (매칭률 {self.match_rate:.1%})\n"
            f"운행 완료      : {self.completed} (완료율 {self.completion_rate:.1%})\n"
            f"거절           : {self.rejected}\n"
            f"평균 대기      : {self.avg_wait_seconds:.0f}초\n"
            f"평균 차내시간  : {self.avg_onboard_seconds:.0f}초\n"
            f"평균 우회배율  : {self.avg_detour_ratio:.2f}x\n"
            f"차량 가동률    : {self.vehicle_utilization:.1%}\n"
            f"총 주행거리    : {self.total_distance_km:.1f} km\n"
        )
