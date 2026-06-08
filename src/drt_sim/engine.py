"""하위호환 shim. 배차 엔진은 :mod:`drt_sim.dispatch.insertion` 으로 이동했다.

기존 import 경로(``from drt_sim.engine import DispatchEngine``)를 유지한다.
"""

from __future__ import annotations

from .dispatch.insertion import DispatchEngine, InsertionPlan

__all__ = ["DispatchEngine", "InsertionPlan"]
