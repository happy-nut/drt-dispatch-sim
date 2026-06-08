"""Redis Streams 백엔드 (실서비스 옵션, "진짜 분산" 모드).

sim 모드(인메모리·가상시간)와 같은 토픽 pub-sub 의미를 Redis Streams 위에서 구현한다.
cluster 모드에서 각 노드를 **실제 별도 OS 프로세스**로 띄울 때 노드 간 통신 채널이 된다.

전달 모델
---------
- ``publish(Message)`` → 해당 토픽 스트림에 ``XADD``.
- ``subscribe(topic, actor_id)`` → 그 토픽의 로컬 구독 actor 등록(+ 스트림 추적).
- ``poll()`` → 구독 스트림을 ``XREAD`` 하여 새 메시지를 바인딩된 런타임의 각 구독 actor
  인박스로 ``deliver`` 한다. 노드 프로세스의 메인 루프에서 주기적으로 호출한다.

여러 프로세스가 같은 스트림을 각자의 last-id 로 ``XREAD`` 하므로 프로세스 간 fan-out 이
이뤄진다. wall-clock 기반이라 결정론은 보장되지 않는다(결정론적 실험은 SimBus 사용).

테스트/오프라인을 위해 ``connect(client=...)`` 로 fakeredis 클라이언트를 주입할 수 있다.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from typing import Dict, List

from .base import Message


class RedisBus:
    """Redis Streams 기반 메시지 버스(노드 프로세스용)."""

    def __init__(self, url: str = "redis://localhost:6379/0", maxlen: int = 10000) -> None:
        self._url = url
        self._maxlen = maxlen
        self._client = None
        self._runtime = None
        self._stream_pos: Dict[str, str] = {}              # topic -> last-read id
        self._subscribers: Dict[str, List[str]] = defaultdict(list)  # topic -> [actor_id]
        self.published = 0
        self.delivered = 0

    # --- 연결/바인딩 ----------------------------------------------------

    def connect(self, client=None) -> "RedisBus":
        """Redis 에 연결한다. ``client`` 를 주입하면(fakeredis 등) 그것을 쓴다."""
        if client is not None:
            self._client = client
            return self
        try:
            import redis
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "redis 패키지가 필요합니다: pip install '.[redis]' (cluster 모드 전용)"
            ) from exc
        self._client = redis.Redis.from_url(self._url, decode_responses=True)
        self._client.ping()
        return self

    def bind(self, runtime) -> None:
        """폴링한 메시지를 전달할 런타임(AsyncioRuntime 등)을 연결한다."""
        self._runtime = runtime

    # --- pub/sub --------------------------------------------------------

    def subscribe(self, topic: str, actor_id: str) -> None:
        if topic not in self._stream_pos:
            # 구독 '시점'의 스트림 끝으로 고정 → 이후 발행 메시지만 받는다.
            # ("$" 는 XREAD 호출 시점에 해석돼 그 직전 메시지를 놓치므로 직접 해석)
            pos = "0-0"
            if self._client is not None:
                try:
                    last = self._client.xrevrange(topic, count=1)
                    if last:
                        pos = last[0][0]
                except Exception:  # pragma: no cover
                    pos = "0-0"
            self._stream_pos[topic] = pos
        if actor_id not in self._subscribers[topic]:
            self._subscribers[topic].append(actor_id)

    def publish(self, message: Message) -> None:
        assert self._client is not None, "connect() 를 먼저 호출해야 합니다"
        self._client.xadd(message.topic, {
            "sender": message.sender,
            "msg_id": str(message.msg_id),
            "trace_id": message.trace_id,
            "redelivery": "1" if message.redelivery else "0",
            "payload": json.dumps(message.payload, default=_json_default),
        }, maxlen=self._maxlen, approximate=True)
        self.published += 1

    def poll(self, block_ms: int = 50, count: int = 256) -> int:
        """구독 스트림에서 새 메시지를 읽어 런타임 구독 actor 들로 전달. 처리 수 반환."""
        assert self._client is not None, "connect() 를 먼저 호출해야 합니다"
        if not self._stream_pos:
            return 0
        resp = self._client.xread(dict(self._stream_pos), count=count, block=block_ms)
        handled = 0
        for topic, entries in resp or []:
            for entry_id, fields in entries:
                self._stream_pos[topic] = entry_id
                msg = Message(
                    topic=topic,
                    payload=json.loads(fields.get("payload", "null")),
                    sender=fields.get("sender", ""),
                    msg_id=int(fields.get("msg_id", "0")),
                    trace_id=fields.get("trace_id", ""),
                    redelivery=fields.get("redelivery") == "1",
                )
                if self._runtime is not None:
                    for actor_id in self._subscribers.get(topic, []):
                        self._runtime.deliver(actor_id, msg)
                        self.delivered += 1
                        handled += 1
        return handled


def _json_default(obj: object) -> object:
    """dataclass payload 직렬화(중첩 포함). json.dumps 가 비직렬화 객체에 대해 호출."""
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)  # type: ignore[arg-type]
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    raise TypeError(f"not JSON serializable: {type(obj)!r}")
