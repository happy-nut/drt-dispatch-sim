"""메시지 버스 패키지: 동일 인터페이스의 sim/redis 백엔드."""

from .base import (
    ASSIGNMENTS,
    CLUSTER_STATE,
    HANDOFF,
    HEARTBEAT,
    RIDE_REQUESTS,
    SHARD_EVENTS,
    SPILL,
    VEHICLE_TELEMETRY,
    Message,
    MessageBus,
    next_msg_id,
    req_topic,
)
from .sim_bus import SimBus

__all__ = [
    "Message",
    "MessageBus",
    "SimBus",
    "next_msg_id",
    "req_topic",
    "RIDE_REQUESTS",
    "VEHICLE_TELEMETRY",
    "ASSIGNMENTS",
    "SHARD_EVENTS",
    "CLUSTER_STATE",
    "HEARTBEAT",
    "HANDOFF",
    "SPILL",
]
