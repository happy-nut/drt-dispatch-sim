"""수요(호출 요청) 스트림 생성기.

PoC: 포아송 도착 과정 + 정사각형 서비스 구역 내 균등 분포 OD(origin-destination).
실제 서비스에서는 시간대별 수요 곡선, 핫스팟 분포, 과거 데이터 리플레이로 교체 가능.
"""

from __future__ import annotations

import random
from typing import Iterator, List

from .geo import Point
from .models import Request


class DemandGenerator:
    def __init__(
        self,
        area_size_km: float = 10.0,
        arrival_rate_per_hour: float = 60.0,
        max_wait: float = 300.0,
        max_detour_factor: float = 1.5,
        seed: int = 0,
    ):
        self.area = area_size_km
        self.rate_per_sec = arrival_rate_per_hour / 3600.0
        self.max_wait = max_wait
        self.max_detour_factor = max_detour_factor
        self._rng = random.Random(seed)
        self._next_id = 0

    def _rand_point(self) -> Point:
        return Point(self._rng.uniform(0, self.area), self._rng.uniform(0, self.area))

    def generate(self, duration_seconds: float) -> List[Request]:
        """[0, duration) 구간의 요청을 도착 시각 순으로 생성."""
        requests: List[Request] = []
        for req in self._stream(duration_seconds):
            requests.append(req)
        return requests

    def _stream(self, duration_seconds: float) -> Iterator[Request]:
        t = 0.0
        while True:
            # 포아송 과정: 다음 도착까지 간격은 지수분포
            gap = self._rng.expovariate(self.rate_per_sec) if self.rate_per_sec > 0 else float("inf")
            t += gap
            if t >= duration_seconds:
                return
            origin = self._rand_point()
            destination = self._rand_point()
            # 동일 지점 OD 회피
            while destination.distance_to(origin) < 0.3:
                destination = self._rand_point()
            yield Request(
                id=self._next_id,
                origin=origin,
                destination=destination,
                request_time=round(t, 2),
                party_size=self._rng.choices([1, 2, 3], weights=[7, 2, 1])[0],
                max_wait=self.max_wait,
                max_detour_factor=self.max_detour_factor,
            )
            self._next_id += 1
