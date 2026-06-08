"""Live(wall-clock asyncio) 클러스터 실행.

가상시간 :class:`Runtime` 대신 :class:`AsyncioRuntime` 위에서 **같은 actor 코드**를
진짜 asyncio 이벤트 루프로 돌린다. 이는 두 가지를 보인다:

1. actor 들이 특정 런타임에 묶여 있지 않다(런타임 이식성).
2. 시스템이 가상시간 트릭 없이 실제 wall-clock 비동기로도 동작한다.

결정론은 보장되지 않는다(실시간 타이밍). 결정론적 실험은 가상시간 모드를 쓴다.
"""

from __future__ import annotations

import asyncio

from .async_runtime import AsyncioRuntime
from .cluster import ClusterController
from .config import SimConfig
from .models import RequestStatus


def run_live(config: SimConfig, virtual_seconds: float = 30.0,
             time_scale: float = 10.0) -> ClusterController:
    """live 모드로 ``virtual_seconds`` 가상초 동안 실행하고 컨트롤러를 반환.

    실제 소요 wall-clock 시간은 ``virtual_seconds / time_scale`` 초.
    """
    rt = AsyncioRuntime(time_scale=time_scale)
    ctrl = ClusterController(config, runtime=rt)
    asyncio.run(rt.run_for(virtual_seconds / time_scale))
    return ctrl


def summarize(ctrl: ClusterController) -> str:
    reqs = list(ctrl.registry.values())
    assigned = sum(1 for r in reqs if r.assigned_vehicle is not None)
    completed = sum(1 for r in reqs if r.status == RequestStatus.COMPLETED)
    rejected = sum(1 for r in reqs if r.status == RequestStatus.REJECTED)
    p50, p95, p99 = ctrl.metrics.percentiles()
    return (
        f"[live(asyncio) 실행] 가상 {ctrl.runtime.now:.0f}s\n"
        f"  리더={ctrl.lease.holder} 샤드맵 v{ctrl._last_shard_map.version}\n"
        f"  요청 {len(reqs)} | 배차 {assigned} | 완료 {completed} | 거절 {rejected}\n"
        f"  배차지연 p50/p95/p99 {p50:.2f}/{p95:.2f}/{p99:.2f}s | "
        f"이중배차 차단 {ctrl.store.blocked_conflicts + ctrl.store.blocked_not_owner}\n"
        f"  버스 전달 {ctrl.bus.metrics.delivered} | 중복무시 "
        f"{sum(w.duplicates_ignored for w in ctrl.workers.values())}"
    )
