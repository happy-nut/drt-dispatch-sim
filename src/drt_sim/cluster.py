"""클러스터 조립 + 시뮬레이션 컨트롤러.

sim 모드의 모든 구성요소(런타임·버스·스토어·게이트웨이·코디네이터·워커·World·메트릭)를
배선하고, 가상시간을 증분 전진시키며, 대시보드용 스냅샷과 런타임 컨트롤(장애 주입 등)을
노출한다. 시드가 같으면 완전히 재현된다.
"""

from __future__ import annotations

import random
from typing import Dict, List, Optional

from .bus import SimBus
from .config import SimConfig
from .coordinator import Coordinator, LeaseRegistry
from .demand import DemandGenerator
from .dispatch.insertion import DispatchEngine
from .dispatch.virtual_stop import VirtualStopSnapper
from .gateway import Gateway
from .geo import GeoProjection, Point
from .live_metrics import LiveMetrics, MetricsActor
from .models import Request, RequestStatus, Vehicle
from .sharding import build_shard_map, gini
from .sim_clock import Runtime
from .store import VehicleStore
from .tracing import Tracer
from .world import World


def build_vehicles(count: int, area_km: float, capacity: int, proj: GeoProjection) -> List[Vehicle]:
    """차량을 운영지역에 격자로 균등 배치하고 home 셀을 부여."""
    vehicles: List[Vehicle] = []
    cols = max(1, int(count ** 0.5))
    for i in range(count):
        r, c = divmod(i, cols)
        x = (c + 0.5) / cols * area_km
        y = (r + 0.5) / cols * area_km
        loc = Point(x, y)
        v = Vehicle(id=i, location=loc, capacity=capacity, home_cell=proj.cell_of(loc))
        vehicles.append(v)
    return vehicles


class ClusterController:
    """sim 클러스터의 수명주기·전진·스냅샷·컨트롤을 관리."""

    def __init__(self, config: SimConfig, runtime=None) -> None:
        self.config = config
        # 기본은 결정론적 가상시간 런타임. live(asyncio) 모드는 AsyncioRuntime 을 주입한다.
        self.runtime = runtime if runtime is not None else Runtime()
        self.tracer = Tracer()
        self.proj = GeoProjection(
            config.area.center_lat, config.area.center_lon,
            config.area.size_km, config.area.h3_resolution,
        )
        self.all_cells = self.proj.cells_covering_area()
        # 셀 중심의 평면 km 좌표(유휴 리밸런싱 선이동 목적지)
        self.cell_centers: Dict[str, Point] = {
            c: Point(*self.proj.to_km(*self.proj.cell_center_latlon(c)))
            for c in self.all_cells
        }

        # 독립적 시드 스트림(재현성)
        self._bus_rng = random.Random(config.seed + 1)
        self.bus = SimBus(
            self.runtime, self._bus_rng, self.tracer,
            base_latency=config.bus.base_latency, jitter=config.bus.jitter,
            loss_prob=config.bus.loss_prob, dup_prob=config.bus.dup_prob,
        )
        self.store = VehicleStore()
        if config.dispatch.engine == "baseline":
            from .dispatch.baseline import NearestIdleEngine
            self.engine = NearestIdleEngine()
        else:
            self.engine = DispatchEngine(max_wait_default=config.demand.max_wait)
        self.registry: Dict[int, Request] = {}
        self.metrics = LiveMetrics()
        self.lease = LeaseRegistry()

        # 노드 id
        self.worker_ids: List[str] = [f"worker-{i}" for i in range(config.cluster.workers)]
        self.coord_ids: List[str] = [f"coord-{i}" for i in range(config.cluster.coordinators)]
        self._next_worker_index = config.cluster.workers

        # 초기 샤드맵 + 차량 소유권
        init_map = build_shard_map(self.all_cells, self.worker_ids, version=0)
        self.vehicles = build_vehicles(
            config.fleet.vehicles, config.area.size_km, config.fleet.capacity, self.proj
        )
        for v in self.vehicles:
            v.owner_node = init_map.owner(v.home_cell)
            self.store.register(v)

        self.demand = DemandGenerator(
            area_size_km=config.area.size_km,
            arrival_rate_per_hour=config.demand.arrival_rate_per_hour,
            max_wait=config.demand.max_wait,
            max_detour_factor=config.demand.max_detour_factor,
            seed=config.seed,
            hourly_profile=config.demand.hourly_profile,
            hotspots=config.demand.hotspots,
        )
        self.snapper: Optional[VirtualStopSnapper] = None
        if config.dispatch.virtual_stops:
            self.snapper = VirtualStopSnapper(
                config.area.size_km, config.dispatch.stop_spacing_km, config.dispatch.max_walk_km
            )

        self.workers: Dict[str, Worker_T] = {}
        self.coords: Dict[str, Coordinator] = {}
        self._recovery_pending: Dict[str, float] = {}  # node -> kill 시각
        self._last_shard_map = init_map            # 리더 부재 시 스냅샷 폴백

        self._spawn_all()
        self._schedule_faults()

    # --- actor 스폰 -----------------------------------------------------

    def _make_worker(self, wid: str):
        from .worker import Worker  # 지연 import (순환 방지)

        c = self.config.cluster
        d = self.config.dispatch
        return Worker(
            wid, self.bus, self.store, self.engine, self.tracer,
            tick=c.worker_tick, throughput_per_tick=c.throughput_per_tick,
            queue_high_water=c.queue_high_water, heartbeat_interval=c.heartbeat_interval,
            cell_centers=self.cell_centers, idle_rebalancing=d.idle_rebalancing,
            rebalance_interval=d.rebalance_interval, rebalance_min_move_km=d.rebalance_min_move_km,
        )

    def _spawn_all(self) -> None:
        c = self.config.cluster
        for wid in self.worker_ids:
            w = self._make_worker(wid)
            self.workers[wid] = w
            self.runtime.spawn(wid, w.run)

        for cid in self.coord_ids:
            coord = Coordinator(
                cid, self.bus, self.store, self.tracer, self.lease,
                self.all_cells, list(self.worker_ids),
                tick=c.lease_renew, lease_ttl=c.lease_ttl,
                heartbeat_timeout=c.heartbeat_timeout,
            )
            self.coords[cid] = coord
            self.runtime.spawn(cid, coord.run)

        gateway = Gateway(
            "gateway", self.bus, self.demand, self.proj, self.tracer,
            self.config.duration, self.registry, snapper=self.snapper,
        )
        self.runtime.spawn("gateway", gateway.run)

        world = World(self.bus, self.store, self.registry, self.tracer,
                      motion_dt=self.config.motion_dt)
        self.runtime.spawn("world", world.run)

        metrics_actor = MetricsActor("metrics", self.bus, self.metrics)
        self.runtime.spawn("metrics", metrics_actor.run)

    # --- 장애 스케줄링(결정론적) ---------------------------------------

    def _schedule_faults(self) -> None:
        for f in self.config.faults:
            self.runtime.schedule_at(f.at, self._make_fault_thunk(f))

    def _make_fault_thunk(self, f):
        def _apply() -> None:
            if f.kind == "kill_worker":
                self.kill_worker(f.target or self.worker_ids[-1])
            elif f.kind == "kill_leader":
                self.kill_leader()
            elif f.kind == "partition":
                self.set_partition(f.nodes)
            elif f.kind == "heal_partition":
                self.heal_partition()
            elif f.kind == "demand_surge":
                self.demand_surge(f.factor)
            elif f.kind == "add_worker":
                self.add_worker()
        return _apply

    # --- 런타임 컨트롤 (대시보드 버튼/시나리오) -------------------------

    def kill_worker(self, node: str) -> None:
        if node in self.workers and self.runtime.is_alive(node):
            self.runtime.kill(node)
            self._recovery_pending[node] = self.runtime.now
            self.tracer.emit(self.runtime.now, "-", "control", "inject_kill_worker", node)

    def kill_leader(self) -> None:
        leader = self.lease.holder
        if leader and self.runtime.is_alive(leader):
            self.runtime.kill(leader)
            self.tracer.emit(self.runtime.now, "-", "control", "inject_kill_leader", leader)

    def set_partition(self, nodes: List[str]) -> None:
        self.bus.set_partition(set(nodes))
        self.tracer.emit(self.runtime.now, "-", "control", "inject_partition", ",".join(nodes))

    def heal_partition(self) -> None:
        self.bus.set_partition(set())
        self.tracer.emit(self.runtime.now, "-", "control", "heal_partition", "")

    def demand_surge(self, factor: float) -> None:
        self.demand.surge_factor = max(0.1, factor)
        self.tracer.emit(self.runtime.now, "-", "control", "inject_surge", f"x{factor}")

    def add_worker(self) -> str:
        wid = f"worker-{self._next_worker_index}"
        self._next_worker_index += 1
        self.worker_ids.append(wid)
        w = self._make_worker(wid)
        self.workers[wid] = w
        self.runtime.spawn(wid, w.run)
        self.tracer.emit(self.runtime.now, "-", "control", "add_worker", wid)
        return wid

    # --- 전진 -----------------------------------------------------------

    def advance(self, virtual_dt: float) -> None:
        """가상시간을 virtual_dt 만큼 전진시키고 메트릭을 샘플링한다."""
        target = self.runtime.now + virtual_dt
        self.runtime.run_until(target)
        self._sample_metrics()
        self._check_recovery()

    def run_batch(self, duration: Optional[float] = None) -> None:
        """배치 실행: duration 까지 한 번에 전진(실험/CLI 용)."""
        dur = duration if duration is not None else self.config.duration
        step = max(1.0, self.config.motion_dt)
        while self.runtime.now < dur:
            self.advance(step)

    def _sample_metrics(self) -> None:
        qdepths = {wid: w.queue_depth for wid, w in self.workers.items()
                   if self.runtime.is_alive(wid)}
        self.metrics.sample(self.runtime.now, qdepths, self.bus.delivery_rate(1.0))

    def _check_recovery(self) -> None:
        if not self._recovery_pending:
            return
        leader = self._leader_coord()
        if leader is None:
            return
        done = []
        for node, t_kill in self._recovery_pending.items():
            if node in leader.down_workers:
                secs = self.runtime.now - t_kill
                self.metrics.recovery_times.append((self.runtime.now, node, secs))
                done.append(node)
        for n in done:
            del self._recovery_pending[n]

    def _leader_coord(self) -> Optional[Coordinator]:
        holder = self.lease.holder
        if holder and holder in self.coords and self.runtime.is_alive(holder):
            return self.coords[holder]
        return None

    # --- 스냅샷 (대시보드) ----------------------------------------------

    def snapshot(self) -> dict:
        return build_snapshot(self)


# Worker 타입 힌트 별칭(지연 import 때문에 문자열로)
Worker_T = "drt_sim.worker.Worker"


def build_snapshot(ctrl: ClusterController) -> dict:
    """대시보드가 렌더링할 클러스터 전체 상태 스냅샷."""
    proj = ctrl.proj
    leader = ctrl._leader_coord()
    if leader is not None:
        ctrl._last_shard_map = leader.shard_map
    shard_map = ctrl._last_shard_map

    # 셀: 경계(km) + 소유 노드(색)
    cells = []
    for cell in ctrl.all_cells:
        owner = shard_map.owner(cell)
        boundary_km = [proj.to_km(lat, lon) for lat, lon in proj.cell_boundary_latlon(cell)]
        cells.append({
            "cell": cell,
            "owner": owner,
            "boundary_km": boundary_km,
        })

    # 차량 (평면 km 좌표 — SVG 카토그래픽 렌더용)
    vehicles = []
    for v in ctrl.store.all_vehicles():
        route_km = [(s.location.x, s.location.y) for s in v.route]
        repo = ([v.reposition_target.x, v.reposition_target.y]
                if v.reposition_target is not None else None)
        vehicles.append({
            "id": v.id, "x": v.location.x, "y": v.location.y, "onboard": v.onboard,
            "owner": v.owner_node, "route_km": route_km, "stops": len(v.route),
            "repo": repo,
        })

    # 노드(워커 + 코디네이터)
    nodes = []
    for wid, w in ctrl.workers.items():
        alive = ctrl.runtime.is_alive(wid)
        nodes.append({
            "id": wid, "role": "worker", "alive": alive,
            "leader": False, "queue_depth": w.queue_depth if alive else 0,
            "processed": w.processed, "rejected": w.rejected,
            "overloaded": w.overloaded if alive else False,
            "cells": len(shard_map.cells_of(wid)),
            "spilled": w.spilled, "duplicates_ignored": w.duplicates_ignored,
        })
    for cid, co in ctrl.coords.items():
        alive = ctrl.runtime.is_alive(cid)
        is_leader = ctrl.lease.holder == cid and alive
        nodes.append({
            "id": cid, "role": "coordinator", "alive": alive,
            "leader": is_leader, "queue_depth": co.spill_backlog if alive else 0,
            "processed": 0, "rejected": 0, "overloaded": False, "cells": 0,
            "spilled": 0, "duplicates_ignored": 0,
        })

    p50, p95, p99 = ctrl.metrics.percentiles()
    reqs = list(ctrl.registry.values())
    completed = sum(1 for r in reqs if r.status == RequestStatus.COMPLETED)
    assigned = sum(1 for r in reqs if r.assigned_vehicle is not None)
    rejected = sum(1 for r in reqs if r.status == RequestStatus.REJECTED)
    onboard = sum(1 for r in reqs if r.status == RequestStatus.ONBOARD)

    qd = {wid: w.queue_depth for wid, w in ctrl.workers.items() if ctrl.runtime.is_alive(wid)}
    recovery = ctrl.metrics.recovery_times[-1][2] if ctrl.metrics.recovery_times else None

    return {
        "sim_time": ctrl.runtime.now,
        "area_km": ctrl.config.area.size_km,
        "cells": cells,
        "vehicles": vehicles,
        "nodes": nodes,
        "leader": ctrl.lease.holder,
        "leader_epoch": ctrl.lease.epoch,
        "shard_version": shard_map.version,
        "down_workers": sorted(leader.down_workers) if leader else [],
        "metrics": {
            "p50": p50, "p95": p95, "p99": p99,
            "total_requests": len(reqs), "completed": completed,
            "assigned": assigned, "rejected": rejected, "onboard": onboard,
            "msg_published": ctrl.bus.metrics.published,
            "msg_delivered": ctrl.bus.metrics.delivered,
            "msg_dropped_loss": ctrl.bus.metrics.dropped_loss,
            "msg_dropped_partition": ctrl.bus.metrics.dropped_partition,
            "msg_duplicated": ctrl.bus.metrics.duplicated,
            "msg_rate": ctrl.bus.delivery_rate(1.0),
            "blocked_conflicts": ctrl.store.blocked_conflicts,
            "blocked_not_owner": ctrl.store.blocked_not_owner,
            "gini_qdepth": gini(list(qd.values())) if qd else 0.0,
            "recovery_secs": recovery,
        },
        "qdepth_timeline": ctrl.metrics.qdepth_timeline[-200:],
        "latency_timeline": ctrl.metrics.latency_timeline[-200:],
        "trace": [e.render() for e in ctrl.tracer.recent(40)],
    }
