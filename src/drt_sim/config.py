"""설정 모델 (pydantic) + YAML 로더.

운영지역, 차량, 워커 수, 샤딩, 수요 프로파일, 장애 시나리오를 한곳에서 선언한다.
모든 무작위성은 ``seed`` 로 제어되어 재현 가능하다.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field


class AreaConfig(BaseModel):
    """운영지역: 중심 위경도 + 한 변 길이(km) + H3 해상도."""

    name: str = "세종시"
    center_lat: float = 36.48
    center_lon: float = 127.28
    size_km: float = 10.0
    h3_resolution: int = 7
    # 지도 베이스 스타일.
    #   "carto-positron"/"open-street-map" 등 → 실제 도로 타일맵(maplibre) 위에 H3 샤드 표시(기본).
    #   "svg" → 외부 타일/WebGL 불필요한 벌집 스키매틱(오프라인·저사양·스크린샷 폴백).
    map_style: str = "carto-positron"


class DemandConfig(BaseModel):
    """수요 생성: 시변 포아송 + 핫스팟."""

    arrival_rate_per_hour: float = 240.0
    max_wait: float = 300.0
    max_detour_factor: float = 1.6
    # 시변 배율: 24개 구간(시간대) 가중치. 길이가 달라도 비례 보간.
    hourly_profile: List[float] = Field(
        default_factory=lambda: [1.0]
    )
    # 핫스팟 (x_km, y_km, weight). 비면 균등 분포.
    hotspots: List[List[float]] = Field(default_factory=list)


class FleetConfig(BaseModel):
    vehicles: int = 30
    capacity: int = 4


class ClusterConfig(BaseModel):
    workers: int = 3
    coordinators: int = 2          # 리더 후보 수
    heartbeat_interval: float = 2.0
    heartbeat_timeout: float = 6.0  # 이 시간 내 헬스 없으면 장애로 간주
    lease_ttl: float = 5.0          # 리더 lease 수명
    lease_renew: float = 2.0        # 리더 lease 갱신 주기
    worker_tick: float = 0.5        # 워커 처리 루프 주기
    throughput_per_tick: int = 6    # 틱당 처리 가능 요청 수(백프레셔 유발 한계)
    queue_high_water: int = 25      # 이 이상이면 overloaded + spill


class BusConfig(BaseModel):
    backend: Literal["sim", "redis"] = "sim"
    base_latency: float = 0.04
    jitter: float = 0.06
    loss_prob: float = 0.0
    dup_prob: float = 0.0
    redis_url: str = "redis://localhost:6379/0"


class FaultEvent(BaseModel):
    """장애/이벤트 주입 한 건(가상시간 기준)."""

    at: float
    kind: Literal[
        "kill_worker", "kill_leader", "partition", "heal_partition",
        "demand_surge", "add_worker",
    ]
    target: Optional[str] = None     # 노드 id (kill_worker 등)
    nodes: List[str] = Field(default_factory=list)  # partition 대상
    factor: float = 1.0              # demand_surge 배율


class DispatchConfig(BaseModel):
    engine: Literal["insertion", "baseline"] = "insertion"
    virtual_stops: bool = True
    stop_spacing_km: float = 0.5
    max_walk_km: float = 0.4
    # 유휴 차량 리밸런싱: 수요 핫셀로 빈 차량 선이동(샤드별 자가 균형)
    idle_rebalancing: bool = True
    rebalance_interval: float = 8.0    # 리밸런싱 평가 주기(가상초)
    rebalance_min_move_km: float = 1.0  # 이보다 가까우면 이동 생략


class SimConfig(BaseModel):
    """최상위 시뮬레이션 설정."""

    seed: int = 42
    duration: float = 1800.0
    real_time_factor: float = 30.0   # 대시보드: 가상 1초당 wall-clock 환산(배속)
    motion_dt: float = 1.0           # 차량 물리 갱신 주기(가상초)

    area: AreaConfig = Field(default_factory=AreaConfig)
    demand: DemandConfig = Field(default_factory=DemandConfig)
    fleet: FleetConfig = Field(default_factory=FleetConfig)
    cluster: ClusterConfig = Field(default_factory=ClusterConfig)
    bus: BusConfig = Field(default_factory=BusConfig)
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)
    faults: List[FaultEvent] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "SimConfig":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    @classmethod
    def from_yaml_or_default(cls, path: Optional[str | Path]) -> "SimConfig":
        if path is None:
            return cls()
        return cls.from_yaml(path)
