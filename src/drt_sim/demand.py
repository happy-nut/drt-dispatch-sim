"""수요(호출 요청) 스트림 생성기.

**시변(time-varying) 포아송 도착** + (선택)**핫스팟 가중 OD**. 시드 기반으로 완전
재현 가능하다.

- 기본 도착률 ``arrival_rate_per_hour`` 에 시간대별 배율 ``hourly_profile`` 을 곱해
  비균질 포아송 과정을 만든다(thinning 으로 구현).
- ``hotspots`` 가 주어지면 승하차 지점이 핫스팟 주변에 몰린다(가우시안). 비면 균등.
"""

from __future__ import annotations

import random
from typing import Iterator, List, Optional, Sequence

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
        hourly_profile: Optional[Sequence[float]] = None,
        hotspots: Optional[Sequence[Sequence[float]]] = None,
    ):
        self.area = area_size_km
        self.base_rate_per_sec = arrival_rate_per_hour / 3600.0
        self.max_wait = max_wait
        self.max_detour_factor = max_detour_factor
        self._rng = random.Random(seed)
        self._next_id = 0
        self.hourly_profile = list(hourly_profile) if hourly_profile else [1.0]
        self.hotspots = [list(h) for h in hotspots] if hotspots else []
        # surge 배율(런타임 컨트롤로 조정 가능)
        self.surge_factor = 1.0

    # --- 시변 강도 ------------------------------------------------------

    def _intensity(self, t: float, duration: float) -> float:
        """시각 t 에서의 순간 도착률(/초)."""
        if duration <= 0 or len(self.hourly_profile) == 1:
            mult = self.hourly_profile[0]
        else:
            frac = (t / duration) * (len(self.hourly_profile) - 1)
            i = int(frac)
            j = min(i + 1, len(self.hourly_profile) - 1)
            w = frac - i
            mult = self.hourly_profile[i] * (1 - w) + self.hourly_profile[j] * w
        return self.base_rate_per_sec * mult * self.surge_factor

    # --- 지점 분포 ------------------------------------------------------

    def _rand_point(self) -> Point:
        if self.hotspots and self._rng.random() < 0.7:
            # 핫스팟 근처(가우시안)
            x0, y0, *_ = self._rng.choices(
                self.hotspots, weights=[h[2] if len(h) > 2 else 1.0 for h in self.hotspots]
            )[0]
            x = min(self.area, max(0.0, self._rng.gauss(x0, self.area * 0.06)))
            y = min(self.area, max(0.0, self._rng.gauss(y0, self.area * 0.06)))
            return Point(x, y)
        return Point(self._rng.uniform(0, self.area), self._rng.uniform(0, self.area))

    # --- 생성 -----------------------------------------------------------

    def generate(self, duration_seconds: float) -> List[Request]:
        """[0, duration) 구간의 요청을 도착 시각 순으로 생성(배치)."""
        return list(self._stream(duration_seconds))

    # 증분(스트리밍) 생성용 공개 래퍼 — 게이트웨이 actor 가 런타임에 사용.
    def intensity(self, t: float, duration: float) -> float:
        """시각 t 의 순간 도착률(/초). surge_factor 반영."""
        return self._intensity(t, duration)

    def sample_gap(self, t: float, duration: float) -> float:
        """현재 강도 기준 다음 도착까지 간격(초). 강도 0이면 매우 큰 값."""
        rate = self._intensity(t, duration)
        if rate <= 0:
            return float("inf")
        return self._rng.expovariate(rate)

    def make_request(self, t: float) -> Request:
        """시각 t 의 요청 한 건 생성(id/RNG 진행)."""
        return self._make_request(t)

    def _stream(self, duration_seconds: float) -> Iterator[Request]:
        # 비균질 포아송: 최대 강도로 후보를 뽑고 thinning 으로 채택.
        peak = self.base_rate_per_sec * max(self.hourly_profile) * max(1.0, self.surge_factor)
        if peak <= 0:
            return
        t = 0.0
        while True:
            t += self._rng.expovariate(peak)
            if t >= duration_seconds:
                return
            if self._rng.random() > self._intensity(t, duration_seconds) / peak:
                continue  # thinning 으로 기각
            yield self._make_request(t)

    def _make_request(self, t: float) -> Request:
        origin = self._rand_point()
        destination = self._rand_point()
        while destination.distance_to(origin) < 0.3:
            destination = self._rand_point()
        req = Request(
            id=self._next_id,
            origin=origin,
            destination=destination,
            request_time=round(t, 2),
            party_size=self._rng.choices([1, 2, 3], weights=[7, 2, 1])[0],
            max_wait=self.max_wait,
            max_detour_factor=self.max_detour_factor,
            trace_id=f"trace-{self._next_id}",
        )
        self._next_id += 1
        return req
