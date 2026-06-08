"""실험: 서비스 품질 집계, 확장성, 베이스라인(비합승) 비교.

모두 배치(가상시간 한 번에 전진) 실행이라 결정론적이다.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional

from .cluster import ClusterController
from .config import SimConfig
from .models import Request, RequestStatus


@dataclass
class RunResult:
    label: str
    total_requests: int
    assigned: int
    completed: int
    rejected: int
    match_rate: float
    pooling_rate: float          # 합승률(다른 승객과 동승한 완료 요청 비율)
    avg_wait: float
    avg_detour_ratio: float
    total_vkt_km: float          # 총 주행거리
    p50: float
    p95: float
    p99: float
    recovery_secs: Optional[float]
    blocked_double_assign: int

    def render(self) -> str:
        rec = f"{self.recovery_secs:.0f}s" if self.recovery_secs is not None else "—"
        return (
            f"[{self.label}]\n"
            f"  요청 {self.total_requests} | 배차 {self.assigned} "
            f"(매칭률 {self.match_rate:.0%}) | 완료 {self.completed} | 거절 {self.rejected}\n"
            f"  합승률 {self.pooling_rate:.0%} | 평균대기 {self.avg_wait:.0f}s | "
            f"평균우회 {self.avg_detour_ratio:.2f}x | VKT {self.total_vkt_km:.1f}km\n"
            f"  배차지연 p50/p95/p99 {self.p50:.2f}/{self.p95:.2f}/{self.p99:.2f}s | "
            f"복구 {rec} | 이중배차 차단 {self.blocked_double_assign}"
        )


def _pooling_rate(requests: List[Request], sim_end: float) -> float:
    """합승률: 실제로 탑승한(픽업된) 요청 중, 같은 차량에서 다른 승객과 차내 시간이
    겹친 비율. 아직 하차 전인 요청은 차내 구간 끝을 sim 종료시각으로 본다."""
    by_vehicle: Dict[int, List[tuple]] = defaultdict(list)
    for r in requests:
        if r.assigned_vehicle is not None and r.pickup_time is not None:
            end = r.dropoff_time if r.dropoff_time is not None else sim_end
            by_vehicle[r.assigned_vehicle].append((r.pickup_time, end, r.id))
    pooled = 0
    total = 0
    for intervals in by_vehicle.values():
        for s, e, i in intervals:
            total += 1
            if any(j != i and s < ee and ss < e for ss, ee, j in intervals):
                pooled += 1
    return pooled / total if total else 0.0


def run_once(config: SimConfig, label: str = "run") -> RunResult:
    ctrl = ClusterController(config)
    ctrl.run_batch(config.duration)
    reqs = list(ctrl.registry.values())
    completed = [r for r in reqs if r.status == RequestStatus.COMPLETED]
    assigned = [r for r in reqs if r.assigned_vehicle is not None]
    rejected = [r for r in reqs if r.status == RequestStatus.REJECTED]

    waits = [r.pickup_time - r.request_time for r in reqs if r.pickup_time is not None]
    detours = [
        (r.dropoff_time - r.pickup_time) / r.direct_travel_time()
        for r in completed
        if r.dropoff_time and r.pickup_time and r.direct_travel_time() > 0
    ]
    vkt = sum(v.distance_traveled for v in ctrl.store.all_vehicles())
    p50, p95, p99 = ctrl.metrics.percentiles()
    recovery = ctrl.metrics.recovery_times[-1][2] if ctrl.metrics.recovery_times else None

    def _avg(xs):
        return sum(xs) / len(xs) if xs else 0.0

    return RunResult(
        label=label,
        total_requests=len(reqs),
        assigned=len(assigned),
        completed=len(completed),
        rejected=len(rejected),
        match_rate=len(assigned) / len(reqs) if reqs else 0.0,
        pooling_rate=_pooling_rate(reqs, config.duration),
        avg_wait=_avg(waits),
        avg_detour_ratio=_avg(detours),
        total_vkt_km=vkt,
        p50=p50, p95=p95, p99=p99,
        recovery_secs=recovery,
        blocked_double_assign=ctrl.store.blocked_conflicts + ctrl.store.blocked_not_owner,
    )


def compare_baseline(config: SimConfig) -> List[RunResult]:
    """합승(insertion) vs 비합승(baseline) — 같은 수요·차량으로 비교."""
    pooled_cfg = config.model_copy(deep=True)
    pooled_cfg.dispatch.engine = "insertion"
    base_cfg = config.model_copy(deep=True)
    base_cfg.dispatch.engine = "baseline"
    return [
        run_once(pooled_cfg, "합승(insertion)"),
        run_once(base_cfg, "비합승(baseline)"),
    ]


def scaling_experiment(config: SimConfig, worker_counts: List[int]) -> List[RunResult]:
    """워커 수를 늘리며 throughput/지연 변화 측정(고정 부하)."""
    results = []
    for n in worker_counts:
        cfg = config.model_copy(deep=True)
        cfg.cluster.workers = n
        results.append(run_once(cfg, f"workers={n}"))
    return results
