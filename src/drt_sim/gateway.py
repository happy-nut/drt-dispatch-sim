"""Request Gateway / Load Generator.

수요 모델에 따라 가상시간에 걸쳐 호출(ride request) 이벤트를 발행한다. 각 요청에 대해:

1. (옵션) 가상 정류장으로 승하차 지점 스냅.
2. 승차지/하차지의 H3 셀 계산.
3. 최신 클러스터 뷰에서 **승차 셀의 소유 워커**를 찾아 그 노드로 직접 라우팅.
   → 소유권 기반 라우팅을 보여주는 지점. 뷰가 아직 없으면 버퍼링했다 도착 시 전송.

surge 컨트롤(수요 폭증)은 ``demand.surge_factor`` 를 런타임에 바꾸면 즉시 반영된다.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .bus import CLUSTER_STATE, Message, SimBus, req_topic
from .demand import DemandGenerator
from .dispatch.virtual_stop import VirtualStopSnapper
from .geo import GeoProjection
from .models import Request
from .protocol import ClusterView
from .sim_clock import ActorContext
from .tracing import Tracer


class Gateway:
    """수요 발생 + 소유권 기반 라우팅 actor."""

    def __init__(
        self,
        node_id: str,
        bus: SimBus,
        demand: DemandGenerator,
        projection: GeoProjection,
        tracer: Tracer,
        duration: float,
        registry: Dict[int, Request],
        *,
        snapper: Optional[VirtualStopSnapper] = None,
    ) -> None:
        self.node_id = node_id
        self.bus = bus
        self.demand = demand
        self.proj = projection
        self.tracer = tracer
        self.duration = duration
        self.registry = registry
        self.snapper = snapper

        self._view: Optional[ClusterView] = None
        self._buffer: List[Request] = []   # 뷰 도착 전 버퍼
        self.emitted = 0

    async def run(self, ctx: ActorContext) -> None:
        self.bus.subscribe(CLUSTER_STATE, self.node_id)
        while ctx.now < self.duration:
            self._drain(ctx)
            gap = self.demand.sample_gap(ctx.now, self.duration)
            if gap == float("inf"):
                await ctx.sleep(1.0)
                continue
            await ctx.sleep(gap)
            self._drain(ctx)
            if ctx.now >= self.duration:
                break
            req = self.demand.make_request(ctx.now)
            self._prepare(req)
            self.registry[req.id] = req
            self.tracer.emit(ctx.now, req.trace_id, self.node_id, "request_emitted",
                             f"cell {req.pickup_cell[:6]} party={req.party_size}", req.id)
            self._route(req, ctx)

    def _drain(self, ctx: ActorContext) -> None:
        while (msg := ctx.try_recv()) is not None:
            if msg.topic == CLUSTER_STATE:
                self._view = msg.payload
                self._flush_buffer(ctx)

    def _prepare(self, req: Request) -> None:
        if self.snapper is not None:
            req.origin = self.snapper.snap(req.origin)
            req.destination = self.snapper.snap(req.destination)
        req.pickup_cell = self.proj.cell_of(req.origin)
        req.dropoff_cell = self.proj.cell_of(req.destination)

    def _route(self, req: Request, ctx: ActorContext) -> None:
        owner = self._view.shard_map.owner(req.pickup_cell) if self._view else None
        if owner is None:
            self._buffer.append(req)
            return
        self.bus.publish(Message(req_topic(owner), req, sender=self.node_id,
                                 trace_id=req.trace_id))
        self.emitted += 1
        self.tracer.emit(ctx.now, req.trace_id, self.node_id, "routed",
                         f"-> {owner}", req.id)

    def _flush_buffer(self, ctx: ActorContext) -> None:
        if not self._view or not self._buffer:
            return
        pending = self._buffer
        self._buffer = []
        for req in pending:
            self._route(req, ctx)
