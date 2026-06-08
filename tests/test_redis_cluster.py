"""진짜 분산(Redis) 제어 플레인 검증.

실제 redis-server 없이도 **공유 FakeServer + 스레드**로 여러 '노드'를 한 프로세스에서
띄워, Redis Streams 하트비트 + Redis lease 리더 선출 + 장애 감지 로직을 검증한다.
실제 실행은 같은 노드 루프를 multiprocessing + redis-server 로 돌린다.
"""

import threading
import time

import pytest

fakeredis = pytest.importorskip("fakeredis")

# importorskip 이후에 임포트(fakeredis 없으면 모듈 전체 skip) — E402 의도적
from drt_sim.bus import HEARTBEAT, Message  # noqa: E402
from drt_sim.bus.redis_bus import RedisBus  # noqa: E402
from drt_sim.cluster_redis import (  # noqa: E402
    RedisLease,
    _Collector,
    run_coordinator_node,
    run_worker_node,
)


def _shared():
    server = fakeredis.FakeServer()
    return lambda: fakeredis.FakeStrictRedis(server=server, decode_responses=True)


def test_redis_lease_single_leader():
    mk = _shared()
    a, b = RedisLease(mk()), RedisLease(mk())
    assert a.try_acquire("coord-0", 2000) is True
    assert b.try_acquire("coord-1", 2000) is False   # 이미 다른 노드가 점유
    assert a.try_acquire("coord-0", 2000) is True     # 소유자 갱신 OK
    assert a.holder() == "coord-0"


def test_redis_lease_expiry_allows_failover():
    mk = _shared()
    a, b = RedisLease(mk()), RedisLease(mk())
    assert a.try_acquire("coord-0", 150) is True
    time.sleep(0.25)                                  # lease 만료
    assert b.try_acquire("coord-1", 2000) is True     # 새 리더 승격
    assert b.holder() == "coord-1"


def test_redisbus_streams_roundtrip():
    mk = _shared()
    pub = RedisBus().connect(client=mk())
    sub = RedisBus().connect(client=mk())
    coll = _Collector()
    sub.bind(coll)
    sub.subscribe(HEARTBEAT, "coord-0")
    pub.publish(Message(HEARTBEAT, {"node": "worker-0", "seq": 1}, sender="worker-0"))
    pub.publish(Message(HEARTBEAT, {"node": "worker-1", "seq": 1}, sender="worker-1"))
    handled = sub.poll(block_ms=20)
    assert handled == 2
    nodes = {m.payload["node"] for m in coll.messages}
    assert nodes == {"worker-0", "worker-1"}


def test_distributed_failure_detection_over_redis():
    """워커 스레드들이 하트비트를 보내고, 한 워커를 멈추면 코디네이터가 down 으로 감지."""
    mk = _shared()
    stop = {w: threading.Event() for w in ("worker-0", "worker-1")}
    threads = [
        threading.Thread(target=run_worker_node, args=("worker-0", mk),
                         kwargs={"duration": 3.0, "hb_interval": 0.1,
                                 "stop_event": stop["worker-0"]}, daemon=True),
        threading.Thread(target=run_worker_node, args=("worker-1", mk),
                         kwargs={"duration": 3.0, "hb_interval": 0.1,
                                 "stop_event": stop["worker-1"]}, daemon=True),
    ]
    result = {}

    def coord():
        result["view"] = run_coordinator_node(
            "coord-0", mk, ["worker-0", "worker-1"],
            duration=2.5, lease_ttl_ms=800, lease_renew=0.15, hb_timeout=0.5,
        )

    ct = threading.Thread(target=coord, daemon=True)
    for t in threads:
        t.start()
    ct.start()
    time.sleep(0.8)
    stop["worker-1"].set()   # worker-1 '프로세스' 종료 → 하트비트 중단
    ct.join(timeout=4.0)
    for t in threads:
        t.join(timeout=1.0)

    view = result.get("view")
    assert view is not None
    assert view["leader"] == "coord-0"        # 리더 선출됨
    assert "worker-0" in view["alive"]        # 살아있는 워커 인지
    assert "worker-1" in view["down"]         # 멈춘 워커 장애 감지
