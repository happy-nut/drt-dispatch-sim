"""Coordinator / Control Plane.

멤버십 관리, **리더 선출(lease 기반)**, 샤드 할당, 노드 join/leave/failure 시
**리밸런싱**, 헬스체크, **클러스터 뷰 발행**, 백프레셔 spill 흡수를 담당한다.

리더 선출
---------
여러 코디네이터 후보가 공유 :class:`LeaseRegistry` 의 lease 를 두고 경쟁한다. 현재
리더만 lease 를 갱신하며 제어 작업(리밸런싱·뷰 발행·spill 재주입)을 수행한다. 리더가
죽으면 lease 가 만료되고 다른 후보가 승격한다. 후보들은 모두 헬스/스필을 추적하므로
페일오버가 매끄럽다.

리밸런싱
--------
하트비트로 멤버십을 추적하고, ``heartbeat_timeout`` 내에 헬스가 없는 워커를 장애로
간주한다. 멤버십이 바뀌면 HRW 로 샤드맵을 재계산하고, 마이그레이션된 셀의 차량
소유권을 스토어에서 이전(버전 증가 → 스테일 쓰기 자동 무효)한 뒤 새 클러스터 뷰를
발행한다.

백프레셔
--------
과부하 워커가 spill 한 요청을 버퍼에 받아, 매 틱 제한된 수만큼 현재 소유 워커로
재주입한다. 수요 폭증을 점진적으로 흡수(graceful degradation)한다.
"""

from __future__ import annotations

from collections import deque
from typing import Callable, Deque, Dict, List, Optional, Set

from .bus import CLUSTER_STATE, HEARTBEAT, SHARD_EVENTS, SPILL, Message, SimBus, req_topic
from .models import Request
from .protocol import ClusterView, Heartbeat, ShardMigration
from .sharding import ShardMap, build_shard_map
from .sim_clock import ActorContext
from .store import VehicleStore
from .tracing import Tracer


class LeaseRegistry:
    """리더 선출용 공유 lease (개념적으로 Redis SETNX+TTL).

    sim 모드에서는 결정론적 공유 객체. cluster 모드에서는 Redis 키로 대체한다.
    """

    def __init__(self) -> None:
        self.holder: Optional[str] = None
        self.expiry: float = 0.0
        self.epoch: int = 0  # 리더가 바뀔 때마다 증가(페일오버 횟수 추적)

    def try_acquire(self, node: str, now: float, ttl: float) -> bool:
        if self.holder is None or now >= self.expiry:
            if self.holder != node:
                self.epoch += 1
            self.holder = node
            self.expiry = now + ttl
            return True
        if self.holder == node:
            self.expiry = now + ttl  # 갱신
            return True
        return False

    def is_leader(self, node: str, now: float) -> bool:
        return self.holder == node and now < self.expiry


class Coordinator:
    """코디네이터 후보 actor (리더가 되면 제어 작업 수행)."""

    def __init__(
        self,
        node_id: str,
        bus: SimBus,
        store: VehicleStore,
        tracer: Tracer,
        lease: LeaseRegistry,
        all_cells: List[str],
        initial_workers: List[str],
        *,
        tick: float = 1.0,
        lease_ttl: float = 5.0,
        heartbeat_timeout: float = 6.0,
        cluster_publish_interval: float = 1.5,
        reinject_per_tick: int = 8,
        on_rebalance: Optional[Callable[[float, List[ShardMigration]], None]] = None,
    ) -> None:
        self.node_id = node_id
        self.bus = bus
        self.store = store
        self.tracer = tracer
        self.lease = lease
        self.all_cells = all_cells
        self.tick = tick
        self.lease_ttl = lease_ttl
        self.heartbeat_timeout = heartbeat_timeout
        self.cluster_publish_interval = cluster_publish_interval
        self.reinject_per_tick = reinject_per_tick
        self.on_rebalance = on_rebalance

        # 멤버십/헬스 상태
        self.last_seen: Dict[str, float] = {w: 0.0 for w in initial_workers}
        self.queue_depths: Dict[str, int] = {w: 0 for w in initial_workers}
        self.alive_workers: Set[str] = set(initial_workers)
        self.down_workers: Set[str] = set()
        self.shard_map: ShardMap = build_shard_map(all_cells, initial_workers, version=0)
        self.is_leader: bool = False
        self._spill_buffer: Deque[Request] = deque()
        self._next_publish = 0.0
        self._bootstrapped = False

    async def run(self, ctx: ActorContext) -> None:
        self.bus.subscribe(HEARTBEAT, self.node_id)
        self.bus.subscribe(SPILL, self.node_id)
        while True:
            # 1) 인박스 처리(후보 전원이 헬스/스필을 추적해 페일오버 대비)
            while (msg := ctx.try_recv()) is not None:
                self._ingest(msg, ctx)
            # 2) 리더 lease 경쟁/갱신
            was_leader = self.is_leader
            self.is_leader = self.lease.try_acquire(self.node_id, ctx.now, self.lease_ttl)
            if self.is_leader and not was_leader:
                self.tracer.emit(ctx.now, "-", self.node_id, "leader_elected",
                                 f"epoch={self.lease.epoch}")
                self._bootstrapped = False  # 새 리더는 즉시 뷰를 발행
            # 3) 리더만 제어 작업 수행
            if self.is_leader:
                self._detect_failures(ctx)
                self._reinject_spill(ctx)
                if ctx.now >= self._next_publish or not self._bootstrapped:
                    self._publish_view(ctx)
                    self._next_publish = ctx.now + self.cluster_publish_interval
                    self._bootstrapped = True
            await ctx.sleep(self.tick)

    # --- 수신 ----------------------------------------------------------

    def _ingest(self, msg: Message, ctx: ActorContext) -> None:
        if msg.topic == HEARTBEAT:
            hb: Heartbeat = msg.payload
            self.last_seen[hb.node] = ctx.now
            self.queue_depths[hb.node] = hb.queue_depth
            if hb.node in self.down_workers:
                # 재가입
                self.down_workers.discard(hb.node)
                self.alive_workers.add(hb.node)
                if self.is_leader:
                    self._rebalance(ctx, "node_join")
            elif hb.node not in self.alive_workers:
                self.alive_workers.add(hb.node)
                if self.is_leader:
                    self._rebalance(ctx, "node_join")
        elif msg.topic == SPILL:
            # 과부하 흡수: 버퍼에 받아 메터링 재주입(리더만 실제로 비움).
            self._spill_buffer.append(msg.payload)

    # --- 장애 감지 & 리밸런싱 -------------------------------------------

    def _detect_failures(self, ctx: ActorContext) -> None:
        newly_down = []
        for w in list(self.alive_workers):
            if ctx.now - self.last_seen.get(w, 0.0) > self.heartbeat_timeout:
                newly_down.append(w)
        if newly_down:
            for w in newly_down:
                self.alive_workers.discard(w)
                self.down_workers.add(w)
                self.tracer.emit(ctx.now, "-", self.node_id, "worker_down",
                                 f"{w} (no heartbeat)")
            self._rebalance(ctx, "node_failure")

    def _rebalance(self, ctx: ActorContext, reason: str) -> None:
        alive = sorted(self.alive_workers)
        if not alive:
            return
        new_map = build_shard_map(self.all_cells, alive, self.shard_map.version + 1)
        migrations: List[ShardMigration] = []
        changed = new_map.diff(self.shard_map)
        for cell, (old, new) in changed.items():
            migrations.append(ShardMigration(new_map.version, ctx.now, cell, old, new, reason))
        # 차량 소유권 이전(버전 증가 → 스테일 커밋 무효), 새 소유자는 복제 상태 승계
        for v in self.store.all_vehicles():
            if v.home_cell is None:
                continue
            new_owner = new_map.owner(v.home_cell)
            if new_owner and v.owner_node != new_owner:
                self.store.reassign_owner(v.id, new_owner)
        self.shard_map = new_map
        for mig in migrations:
            self.bus.publish(Message(SHARD_EVENTS, mig, sender=self.node_id))
        self.tracer.emit(ctx.now, "-", self.node_id, "rebalance",
                         f"{reason}: {len(migrations)} cells -> v{new_map.version}")
        if self.on_rebalance:
            self.on_rebalance(ctx.now, migrations)
        self._publish_view(ctx)

    # --- 백프레셔 재주입 ------------------------------------------------

    def _reinject_spill(self, ctx: ActorContext) -> None:
        n = 0
        while self._spill_buffer and n < self.reinject_per_tick:
            req = self._spill_buffer.popleft()
            owner = self.shard_map.owner(req.pickup_cell) if req.pickup_cell else None
            if owner:
                self.bus.publish(Message(req_topic(owner), req, sender=self.node_id,
                                         trace_id=req.trace_id))
            n += 1

    # --- 클러스터 뷰 발행 -----------------------------------------------

    def _publish_view(self, ctx: ActorContext) -> None:
        view = ClusterView(
            version=self.shard_map.version,
            sim_time=ctx.now,
            leader=self.node_id,
            shard_map=self.shard_map,
            alive_workers=sorted(self.alive_workers),
            down_workers=sorted(self.down_workers),
            queue_depths=dict(self.queue_depths),
        )
        self.bus.publish(Message(CLUSTER_STATE, view, sender=self.node_id))

    @property
    def spill_backlog(self) -> int:
        return len(self._spill_buffer)
