"""Dispatch Worker (노드).

운영지역을 분할한 **지오 샤드(H3 셀 집합)**를 소유하고, 자기 샤드에 들어온 요청을
자기 차량에 매칭한다. 수평 확장의 기본 단위.

핵심 책임
---------
- **소유권 기반 매칭**: 스토어에서 자기가 단일 라이터인 차량만 후보로 본다.
- **이중 배차 방지**: 배차는 낙관적 커밋(``commit_assignment``)으로만 확정. 소유권/
  버전 검사를 통과 못하면 거부되고 재시도된다.
- **멱등성**: 같은 request_id 의 중복 전달(at-least-once)은 한 번만 처리.
- **백프레셔**: 틱당 처리량 한계 → 초과분은 큐에 쌓이고, 고수위를 넘으면 코디네이터로
  spill(부하 재분산).
- **결과적 일관성 대응**: 스테일 라우팅으로 자기 소유가 아닌 셀의 요청을 받으면 현재
  소유 노드로 재라우팅(handoff).
- **크로스 샤드 핸드오프**: 하차지가 다른 샤드면 그 노드에 인지 이벤트를 보낸다.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional, Set

from .bus import (
    ASSIGNMENTS,
    CLUSTER_STATE,
    HANDOFF,
    HEARTBEAT,
    SPILL,
    Message,
    SimBus,
    req_topic,
)
from .dispatch.insertion import DispatchEngine, InsertionPlan
from .geo import Point
from .models import Request, RequestStatus
from .protocol import AssignmentConfirmed, ClusterView, Handoff, Heartbeat
from .sim_clock import ActorContext
from .store import VehicleStore
from .tracing import Tracer


class Worker:
    """배차 워커 노드 actor."""

    def __init__(
        self,
        node_id: str,
        bus: SimBus,
        store: VehicleStore,
        engine: DispatchEngine,
        tracer: Tracer,
        *,
        tick: float = 0.5,
        throughput_per_tick: int = 6,
        queue_high_water: int = 25,
        heartbeat_interval: float = 2.0,
        cell_centers: Optional[Dict[str, Point]] = None,
        idle_rebalancing: bool = True,
        rebalance_interval: float = 8.0,
        rebalance_min_move_km: float = 1.0,
    ) -> None:
        self.node_id = node_id
        self.bus = bus
        self.store = store
        self.engine = engine
        self.tracer = tracer
        self.tick = tick
        self.throughput_per_tick = throughput_per_tick
        self.queue_high_water = queue_high_water
        self.heartbeat_interval = heartbeat_interval
        self.cell_centers = cell_centers or {}
        self.idle_rebalancing = idle_rebalancing
        self.rebalance_interval = rebalance_interval
        self.rebalance_min_move_km = rebalance_min_move_km

        # 상태 (대시보드가 읽음)
        self.pending: Deque[Request] = deque()
        self.seen: Set[int] = set()         # 멱등성 dedupe
        self.processed: int = 0
        self.rejected: int = 0
        self.spilled: int = 0
        self.duplicates_ignored: int = 0
        self.owned_cells: Set[str] = set()
        self.leader: str = ""
        self.view_version: int = -1
        self.overloaded: bool = False
        self._view: Optional[ClusterView] = None
        # 유휴 리밸런싱: 셀별 최근 수요(지수감쇠)
        self._demand: Dict[str, float] = defaultdict(float)
        self._next_rebalance: float = 0.0
        self.repositioned: int = 0

    @property
    def queue_depth(self) -> int:
        return len(self.pending)

    def subscriptions(self) -> List[str]:
        return [req_topic(self.node_id), CLUSTER_STATE, HANDOFF]

    # --- actor 메인 루프 ------------------------------------------------

    async def run(self, ctx: ActorContext) -> None:
        for topic in self.subscriptions():
            self.bus.subscribe(topic, self.node_id)
        next_hb = 0.0
        while True:
            # 1) 인박스 비우기(제어 메시지는 즉시, 요청은 큐로)
            while (msg := ctx.try_recv()) is not None:
                self._ingest(msg, ctx)
            # 2) 백로그 처리(틱당 처리량 한계)
            self._process_batch(ctx)
            # 3) 백프레셔 spill
            self._maybe_spill(ctx)
            # 4) 유휴 차량 리밸런싱(수요 핫셀로 선이동)
            if self.idle_rebalancing and ctx.now >= self._next_rebalance:
                self._rebalance_idle(ctx)
                self._next_rebalance = ctx.now + self.rebalance_interval
            # 5) 헬스 비트
            if ctx.now >= next_hb:
                self._send_heartbeat(ctx)
                next_hb = ctx.now + self.heartbeat_interval
            await ctx.sleep(self.tick)

    # --- 수신 처리 ------------------------------------------------------

    def _ingest(self, msg: Message, ctx: ActorContext) -> None:
        if msg.topic == CLUSTER_STATE:
            view: ClusterView = msg.payload
            self._view = view
            self.leader = view.leader
            self.view_version = view.version
            self.owned_cells = set(view.shard_map.cells_of(self.node_id))
            return
        if msg.topic == HANDOFF:
            ho: Handoff = msg.payload
            if ho.to_node != self.node_id:
                return  # 나에게 온 인지 이벤트만 처리(브로드캐스트 필터)
            self.tracer.emit(
                ctx.now, f"trace-{ho.request_id}", self.node_id, "handoff_recv",
                f"inbound from {ho.from_node} (cell {ho.dropoff_cell[:6]})", ho.request_id,
            )
            return
        if msg.topic == req_topic(self.node_id):
            req: Request = msg.payload
            if req.id in self.seen:
                self.duplicates_ignored += 1  # 멱등성: 중복 전달 무시
                return
            self.seen.add(req.id)
            self.pending.append(req)

    # --- 배치 처리 ------------------------------------------------------

    def _process_batch(self, ctx: ActorContext) -> None:
        budget = self.throughput_per_tick
        deferred: List[Request] = []
        while budget > 0 and self.pending:
            req = self.pending.popleft()
            budget -= 1
            # 스테일 라우팅: 내가 더는 이 셀의 소유자가 아니면 현재 소유자로 재라우팅
            owner = self._view.shard_map.owner(req.pickup_cell) if self._view else None
            if owner and owner != self.node_id:
                self._reroute(req, owner, ctx)
                continue
            self._try_dispatch(req, ctx)
        # 처리 못한 잔여는 순서 유지하며 되돌림
        for r in reversed(deferred):
            self.pending.appendleft(r)

    def _try_dispatch(self, req: Request, ctx: ActorContext) -> None:
        candidates = self.store.vehicles_owned_by(self.node_id)
        plan: Optional[InsertionPlan] = self.engine.dispatch(req, candidates, ctx.now)
        if plan is None:
            req.status = RequestStatus.REJECTED
            self.rejected += 1
            # 미충족 수요(공급 부족) 신호 — 유휴 차량을 이 셀로 선이동시킬 근거.
            if req.pickup_cell is not None:
                self._demand[req.pickup_cell] += 1.0
            self.tracer.emit(ctx.now, req.trace_id, self.node_id, "rejected",
                             "no feasible vehicle", req.id)
            return
        result = self.store.commit_assignment(
            plan.vehicle_id, plan.expected_version, plan.new_route, by_node=self.node_id
        )
        if not result.ok:
            # 이중 배차 차단됨 → 재시도(다음 틱)
            self.pending.append(req)
            self.seen.discard(req.id)  # 재처리 허용
            self.tracer.emit(ctx.now, req.trace_id, self.node_id, "commit_blocked",
                             result.reason, req.id)
            return

        veh = self.store.get(plan.vehicle_id)
        if veh is not None:
            veh.reposition_target = None  # 배차되면 유휴 선이동 중단
        req.status = RequestStatus.ASSIGNED
        req.assigned_vehicle = plan.vehicle_id
        req.assigned_at = ctx.now
        self.processed += 1
        latency = ctx.now - req.request_time
        self.tracer.emit(ctx.now, req.trace_id, self.node_id, "assigned",
                         f"veh#{plan.vehicle_id} +{plan.added_cost:.0f}s", req.id)
        self.bus.publish(Message(
            ASSIGNMENTS,
            AssignmentConfirmed(req.id, plan.vehicle_id, self.node_id, ctx.now, latency),
            sender=self.node_id, trace_id=req.trace_id,
        ))
        # 크로스 샤드 핸드오프
        self._maybe_handoff(req, ctx)

    def _maybe_handoff(self, req: Request, ctx: ActorContext) -> None:
        if not self._view or req.dropoff_cell is None:
            return
        drop_owner = self._view.shard_map.owner(req.dropoff_cell)
        if drop_owner and drop_owner != self.node_id:
            self.bus.publish(Message(
                HANDOFF,
                Handoff(req.id, self.node_id, drop_owner, req.dropoff_cell, ctx.now),
                sender=self.node_id, trace_id=req.trace_id,
            ))
            self.tracer.emit(ctx.now, req.trace_id, self.node_id, "handoff_send",
                             f"-> {drop_owner}", req.id)

    def _reroute(self, req: Request, owner: str, ctx: ActorContext) -> None:
        self.seen.discard(req.id)
        self.bus.publish(Message(req_topic(owner), req, sender=self.node_id,
                                 trace_id=req.trace_id))
        self.tracer.emit(ctx.now, req.trace_id, self.node_id, "reroute",
                         f"stale owner -> {owner}", req.id)

    # --- 백프레셔 -------------------------------------------------------

    def _maybe_spill(self, ctx: ActorContext) -> None:
        self.overloaded = len(self.pending) > self.queue_high_water
        if not self.overloaded:
            return
        # 고수위 초과분을 코디네이터로 spill
        while len(self.pending) > self.queue_high_water:
            req = self.pending.pop()
            self.seen.discard(req.id)
            self.spilled += 1
            self.bus.publish(Message(SPILL, req, sender=self.node_id, trace_id=req.trace_id))
            self.tracer.emit(ctx.now, req.trace_id, self.node_id, "spill",
                             "overloaded -> coordinator", req.id)

    # --- 유휴 차량 리밸런싱 ---------------------------------------------

    def _rebalance_idle(self, ctx: ActorContext) -> None:
        """승객 없는 유휴 차량을 **미충족 수요(거절 발생)** 셀로 선이동시킨다.

        거절은 그 셀에 공급이 부족했다는 신호다. 매칭이 충분하면(거절 0) 아무 차량도
        움직이지 않아 deadhead 오버헤드가 없다. 각 워커가 자기 샤드 안에서만 수행하므로
        전역 조율 없이 분산적으로 동작하고, 신호는 지수감쇠한다.
        """
        # 수요 감쇠
        for c in list(self._demand):
            self._demand[c] *= 0.85
            if self._demand[c] < 0.1:
                del self._demand[c]
        if not self.cell_centers:
            return
        idle = [v for v in self.store.vehicles_owned_by(self.node_id)
                if not v.route and v.reposition_target is None]
        if not idle:
            return
        ranked = sorted(
            (c for c in self.owned_cells if self._demand.get(c, 0.0) > 0.5),
            key=lambda c: self._demand[c], reverse=True,
        )
        targets = [self.cell_centers[c] for c in ranked if c in self.cell_centers]
        if not targets:
            return
        for i, v in enumerate(idle):
            tgt = targets[i % len(targets)]
            if v.location.distance_to(tgt) > self.rebalance_min_move_km:
                v.reposition_target = tgt
                self.repositioned += 1
                self.tracer.emit(ctx.now, "-", self.node_id, "rebalance_idle",
                                 f"veh#{v.id} -> 수요셀 {ranked[i % len(targets)][:6]}")

    def _send_heartbeat(self, ctx: ActorContext) -> None:
        self.bus.publish(Message(
            HEARTBEAT,
            Heartbeat(self.node_id, ctx.now, len(self.pending), self.processed, self.overloaded),
            sender=self.node_id,
        ))
