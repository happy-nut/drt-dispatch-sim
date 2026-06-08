"""대시보드용 시뮬레이션 러너 (백그라운드 스레드).

:class:`ClusterController` 를 별도 스레드에서 **배속(real_time_factor)**으로 전진시키고,
Dash 콜백이 락 아래에서 스냅샷을 읽거나 컨트롤(장애 주입 등)을 적용한다. 컨트롤러
내부는 단일 스레드 가상시간 엔진이므로, 락은 advance 와 snapshot/control 의 상호배제만
보장하면 된다.
"""

from __future__ import annotations

import threading
import time
from typing import List

from ..cluster import ClusterController
from ..config import SimConfig


class SimRunner:
    def __init__(self, config: SimConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._ctrl = ClusterController(config)
        self.playing = True
        self.speed = 1.0  # 배속 슬라이더 배율
        self._stop = False
        self._wall_tick = 0.1  # wall-clock 갱신 간격(초)
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop:
            if self.playing and self._ctrl.runtime.now < self.config.duration:
                dv = self.config.real_time_factor * self.speed * self._wall_tick
                with self._lock:
                    self._ctrl.advance(dv)
            time.sleep(self._wall_tick)

    # --- 대시보드 인터페이스 -------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return self._ctrl.snapshot()

    def set_playing(self, playing: bool) -> None:
        self.playing = playing

    def set_speed(self, speed: float) -> None:
        self.speed = max(0.1, speed)

    def kill_worker(self, node: str) -> None:
        with self._lock:
            self._ctrl.kill_worker(node)

    def kill_leader(self) -> None:
        with self._lock:
            self._ctrl.kill_leader()

    def add_worker(self) -> None:
        with self._lock:
            self._ctrl.add_worker()

    def demand_surge(self, factor: float) -> None:
        with self._lock:
            self._ctrl.demand_surge(factor)

    def partition(self, nodes: List[str]) -> None:
        with self._lock:
            self._ctrl.set_partition(nodes)

    def heal_partition(self) -> None:
        with self._lock:
            self._ctrl.heal_partition()

    def reset(self, seed: int | None = None) -> None:
        with self._lock:
            if seed is not None:
                self.config = self.config.model_copy(update={"seed": seed})
            self._ctrl = ClusterController(self.config)

    def worker_ids(self) -> List[str]:
        with self._lock:
            return list(self._ctrl.worker_ids)
