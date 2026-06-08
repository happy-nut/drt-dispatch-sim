"""차량 상태 스토어: 샤드별 단일 라이터 + 낙관적 동시성 + 복제.

이 모듈이 **이중 배차 방지(no-double-assignment)**의 핵심이다.

규칙
----
1. 모든 차량은 정확히 한 노드(``owner_node``)가 소유하는 **단일 라이터**다.
2. 배차 커밋은 ``commit_assignment(vehicle_id, expected_version, ..., by_node)`` 로만
   가능하며 다음을 모두 만족해야 성공한다:
   - 차량이 존재할 것
   - ``by_node`` 가 현재 소유자일 것 (다른 노드의 쓰기를 차단)
   - ``expected_version`` 이 현재 버전과 일치할 것 (낙관적 동시성 — 같은 차량을
     동시에 노리는 두 쓰기 중 하나만 성공)
   성공하면 버전을 +1 한다.
3. 커밋마다 **팔로워 복제본**에 상태를 복사한다. 소유 노드가 죽어 샤드가 다른 노드로
   재할당되면, 새 소유자는 복제본에 보존된 차량/경로 상태에서 복구한다.

sim 모드에서는 논리적으로 샤딩된 스토어를 하나의 객체로 합쳐 두지만 쓰기 경로에서
소유권을 강제하므로 단일 라이터 의미가 그대로 유지된다. 실배포에서는 샤드별로 별도
단일 라이터 프로세스 + 팔로워가 된다(README 트레이드오프 참고).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Dict, List, Optional

from .models import RouteStop, Vehicle


@dataclass(frozen=True)
class CommitResult:
    """배차 커밋 결과."""

    ok: bool
    reason: str           # "" | "not_found" | "not_owner" | "version_conflict"
    version: int          # 커밋 후(또는 현재) 버전


class VehicleStore:
    """차량 상태의 권위 있는(authoritative) 저장소."""

    def __init__(self) -> None:
        self._vehicles: Dict[int, Vehicle] = {}
        # 팔로워 복제본: 소유 노드 장애 시 복구 소스.
        self._replica: Dict[int, Vehicle] = {}
        # 거부된 커밋 카운터(이중 배차 시도가 차단된 횟수 — 시각 증거).
        self.blocked_conflicts = 0
        self.blocked_not_owner = 0

    # --- 등록/조회 ------------------------------------------------------

    def register(self, vehicle: Vehicle) -> None:
        self._vehicles[vehicle.id] = vehicle
        self._replicate(vehicle)

    def get(self, vehicle_id: int) -> Optional[Vehicle]:
        return self._vehicles.get(vehicle_id)

    def all_vehicles(self) -> List[Vehicle]:
        return list(self._vehicles.values())

    def vehicles_owned_by(self, node: str) -> List[Vehicle]:
        """해당 노드가 소유한(단일 라이터) 차량 목록 — 워커가 매칭 후보로 쓴다."""
        return [v for v in self._vehicles.values() if v.owner_node == node]

    # --- 쓰기 (단일 라이터 + 낙관적 동시성) -----------------------------

    def commit_assignment(
        self,
        vehicle_id: int,
        expected_version: int,
        new_route: List[RouteStop],
        by_node: str,
    ) -> CommitResult:
        """경로 갱신을 시도한다. 소유권·버전 검사를 통과해야만 적용된다."""
        v = self._vehicles.get(vehicle_id)
        if v is None:
            return CommitResult(False, "not_found", -1)
        if v.owner_node != by_node:
            # 다른 노드가 내 차량을 건드리려 함 → 차단(이중 배차 방지).
            self.blocked_not_owner += 1
            return CommitResult(False, "not_owner", v.version)
        if v.version != expected_version:
            # 같은 차량에 대한 경쟁 쓰기 → 한쪽만 성공.
            self.blocked_conflicts += 1
            return CommitResult(False, "version_conflict", v.version)

        v.route = new_route
        v.version += 1
        self._replicate(v)
        return CommitResult(True, "", v.version)

    # --- 소유권 이전 (리밸런싱/페일오버) --------------------------------

    def reassign_owner(self, vehicle_id: int, new_owner: str) -> None:
        """샤드 마이그레이션: 차량 소유자를 바꾸고 버전을 올린다.

        버전을 올리므로 옛 소유자가 보낸 in-flight 스테일 커밋은 자동으로 실패한다.
        새 소유자는 (복제본에 보존된) 현재 경로/탑승 상태를 그대로 이어받는다.
        """
        v = self._vehicles.get(vehicle_id)
        if v is None:
            return
        v.owner_node = new_owner
        v.version += 1
        self._replicate(v)

    def recover_from_replica(self, vehicle_id: int) -> Optional[Vehicle]:
        """복제본에서 차량 상태를 복구한다(새 소유자의 장애 복구 경로)."""
        rep = self._replica.get(vehicle_id)
        if rep is None:
            return None
        restored = copy.deepcopy(rep)
        self._vehicles[vehicle_id] = restored
        return restored

    # --- 복제 -----------------------------------------------------------

    def _replicate(self, vehicle: Vehicle) -> None:
        # 팔로워로의 비동기 복제를 단순화: 커밋 시점에 깊은 복사 스냅샷 보관.
        self._replica[vehicle.id] = copy.deepcopy(vehicle)

    def replica_version(self, vehicle_id: int) -> int:
        rep = self._replica.get(vehicle_id)
        return rep.version if rep else -1
