"""진짜 분산(cluster) 모드 — 실제 별도 OS 프로세스 + Redis Streams.

각 노드를 **독립 OS 프로세스**(multiprocessing)로 띄우고, Redis Streams 와 Redis 키
기반 lease 로 **제어 플레인 분산 조율**(하트비트·리더 선출·멤버십·장애 감지)을 실제
프로세스 간에 수행한다. sim 모드(결정론·대시보드)와 대비되는 "진짜 분산" 데모다.

데이터 플레인(차량 스토어·배차·물리)은 결정론을 위해 sim 모드에 둔다. 이 모드는 분산
시스템의 가장 어려운 부분(리더 선출·장애 감지·멤버십)이 실제 프로세스 경계를 넘어
동작함을 보인다.

테스트 가능성
-------------
노드 루프는 ``make_client`` 콜러블을 받으므로, 테스트에서는 공유 ``FakeServer`` 에 붙는
fakeredis 클라이언트를 주입해 **여러 노드를 스레드로** 한 프로세스에서 검증할 수 있다.
실제 실행은 같은 루프를 multiprocessing 프로세스 + 실제 redis-server 로 돌린다.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable, Dict, List, Optional

from .bus import HEARTBEAT, Message
from .bus.redis_bus import RedisBus

LEADER_KEY = "drt:leader"
CLUSTER_VIEW_STREAM = "cluster_view"


class RedisLease:
    """Redis ``SET NX PX`` 기반 리더 lease(프로세스 간 리더 선출)."""

    def __init__(self, client, key: str = LEADER_KEY) -> None:
        self._c = client
        self.key = key

    def try_acquire(self, node: str, ttl_ms: int) -> bool:
        if self._c.set(self.key, node, nx=True, px=ttl_ms):
            return True
        if self.holder() == node:
            self._c.set(self.key, node, px=ttl_ms)  # 갱신(소유자만)
            return True
        return False

    def holder(self) -> Optional[str]:
        v = self._c.get(self.key)
        return v.decode() if isinstance(v, bytes) else v


class _Collector:
    """RedisBus.poll 이 전달하는 메시지를 모으는 최소 런타임 대체."""

    def __init__(self) -> None:
        self.messages: List[Message] = []

    def deliver(self, actor_id: str, msg: Message) -> None:
        self.messages.append(msg)

    def is_alive(self, actor_id: str) -> bool:
        return True


def run_worker_node(
    node_id: str,
    make_client: Callable[[], object],
    *,
    duration: float,
    hb_interval: float = 0.3,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """워커 노드 프로세스: 주기적으로 하트비트를 Redis 스트림에 발행."""
    client = make_client()
    bus = RedisBus().connect(client=client)
    t0 = time.time()
    seq = 0
    while time.time() - t0 < duration:
        if stop_event is not None and stop_event.is_set():
            return
        bus.publish(Message(HEARTBEAT, {"node": node_id, "ts": time.time(), "seq": seq},
                            sender=node_id))
        seq += 1
        time.sleep(hb_interval)


def run_coordinator_node(
    node_id: str,
    make_client: Callable[[], object],
    worker_ids: List[str],
    *,
    duration: float,
    lease_ttl_ms: int = 800,
    lease_renew: float = 0.3,
    hb_timeout: float = 1.0,
    stop_event: Optional[threading.Event] = None,
    on_view: Optional[Callable[[dict], None]] = None,
) -> dict:
    """코디네이터 노드 프로세스: 리더 경쟁 + 하트비트 추적 + 장애 감지 + 뷰 발행.

    마지막 클러스터 뷰(dict)를 반환한다.
    """
    client = make_client()
    bus = RedisBus().connect(client=client)
    bus.bind(_Collector())
    collector: _Collector = bus._runtime  # type: ignore[assignment]
    bus.subscribe(HEARTBEAT, node_id)
    lease = RedisLease(client)

    last_seen: Dict[str, float] = {}
    view = {"leader": None, "alive": list(worker_ids), "down": [], "version": 0}
    t0 = time.time()
    while time.time() - t0 < duration:
        if stop_event is not None and stop_event.is_set():
            break
        is_leader = lease.try_acquire(node_id, lease_ttl_ms)
        # 하트비트 수집
        bus.poll(block_ms=50)
        now = time.time()
        for msg in collector.messages:
            hb = msg.payload
            if isinstance(hb, dict) and "node" in hb:
                last_seen[hb["node"]] = now
        collector.messages.clear()
        if is_leader:
            alive, down = [], []
            for w in worker_ids:
                seen = last_seen.get(w)
                if seen is not None and now - seen <= hb_timeout:
                    alive.append(w)
                else:
                    down.append(w)
            new_view = {"leader": node_id, "alive": alive, "down": down,
                        "version": view["version"] + (1 if (alive, down) !=
                                   (view["alive"], view["down"]) else 0)}
            if (new_view["alive"], new_view["down"]) != (view["alive"], view["down"]):
                client.xadd(CLUSTER_VIEW_STREAM, {"view": json.dumps(new_view)},
                            maxlen=1000, approximate=True)
                if on_view is not None:
                    on_view(new_view)
            view = new_view
        time.sleep(lease_renew)
    return view


def launch_redis_cluster(
    workers: int = 3,
    coordinators: int = 2,
    duration: float = 8.0,
    kill_worker_at: Optional[float] = 4.0,
    redis_url: str = "redis://localhost:6379/0",
) -> None:
    """실제 OS 프로세스로 코디네이터/워커를 띄운다(redis-server 필요).

    중간에 한 워커 프로세스를 종료해 코디네이터의 장애 감지·뷰 갱신을 보여준다.
    """
    import multiprocessing as mp

    try:
        import redis
        redis.Redis.from_url(redis_url, decode_responses=True).ping()
    except Exception as exc:  # pragma: no cover - redis 미가동 환경
        raise SystemExit(
            f"redis-server 에 연결할 수 없습니다({redis_url}): {exc}\n"
            f"  brew install redis && redis-server  로 띄운 뒤 다시 실행하세요."
        )

    worker_ids = [f"worker-{i}" for i in range(workers)]
    coord_ids = [f"coord-{i}" for i in range(coordinators)]

    def make_client():
        import redis
        return redis.Redis.from_url(redis_url, decode_responses=True)

    procs: List[mp.Process] = []
    for cid in coord_ids:
        procs.append(mp.Process(target=run_coordinator_node,
                                args=(cid, make_client, worker_ids),
                                kwargs={"duration": duration}, name=cid))
    worker_procs: Dict[str, mp.Process] = {}
    for wid in worker_ids:
        p = mp.Process(target=run_worker_node, args=(wid, make_client),
                       kwargs={"duration": duration}, name=wid)
        worker_procs[wid] = p
        procs.append(p)

    print(f"실제 OS 프로세스 {len(procs)}개 기동 (coord {coordinators}, worker {workers})")
    for p in procs:
        p.start()

    if kill_worker_at is not None and worker_ids:
        time.sleep(kill_worker_at)
        victim = worker_ids[-1]
        print(f"[kill] {victim} 프로세스(pid={worker_procs[victim].pid}) 강제 종료 → 장애 감지 유도")
        worker_procs[victim].terminate()

    for p in procs:
        p.join()
    print("클러스터 종료. 코디네이터 로그에서 리더 선출과 장애 감지를 확인하세요.")
