"""지오 샤딩 & 파티션 소유권.

운영지역을 덮는 H3 셀들을 워커 노드에 매핑한다. 한 노드가 소유한 셀 집합이 그
노드의 **샤드**다. 매핑은 **HRW(Highest-Random-Weight, rendezvous) 해싱**으로
계산한다.

왜 HRW 인가 (consistent hashing 대비)
------------------------------------
- 노드가 추가/제거되어도 **셀의 일부만** 다른 노드로 옮겨간다(평균 1/N 만 이동).
  → 페일오버 시 샤드 마이그레이션이 최소화되고 시각적으로도 "일부 셀만 다시 칠해짐".
- 가상 노드 링 관리 없이 셀·노드 id 만으로 결정론적으로 계산된다.
- 노드 집합이 같으면 항상 같은 매핑 → 모든 노드가 코디네이터 뷰 없이도 동일 결론.

해시는 표준 라이브러리(hashlib)만 사용해 플랫폼 간 결정론을 보장한다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Sequence


def _hash_weight(cell: str, node: str) -> int:
    """(cell, node) 쌍의 결정론적 가중치. 가장 큰 노드가 셀을 소유한다."""
    h = hashlib.sha256(f"{cell}|{node}".encode()).digest()
    return int.from_bytes(h[:8], "big")


def owner_of_cell(cell: str, nodes: Sequence[str]) -> str:
    """HRW: 살아있는 노드 중 (cell,node) 가중치가 최대인 노드가 셀을 소유."""
    if not nodes:
        raise ValueError("no nodes available to own cell")
    return max(nodes, key=lambda n: _hash_weight(cell, n))


@dataclass(frozen=True)
class ShardMap:
    """클러스터 뷰의 핵심: 셀 -> 소유 노드 매핑 + 버전.

    코디네이터(리더)가 멤버십 변화마다 새 버전을 발행한다. 워커/게이트웨이/대시보드는
    이 뷰를 보고 라우팅·소유권을 판단한다. ``version`` 으로 staleness 를 드러낸다.
    """

    version: int
    cell_owner: Dict[str, str]
    nodes: List[str] = field(default_factory=list)

    def owner(self, cell: str) -> str | None:
        return self.cell_owner.get(cell)

    def cells_of(self, node: str) -> List[str]:
        return sorted(c for c, n in self.cell_owner.items() if n == node)

    def load_per_node(self) -> Dict[str, int]:
        """노드별 소유 셀 수(정적 부하 분포)."""
        out: Dict[str, int] = {n: 0 for n in self.nodes}
        for n in self.cell_owner.values():
            out[n] = out.get(n, 0) + 1
        return out

    def diff(self, other: "ShardMap") -> Dict[str, tuple[str | None, str | None]]:
        """이전 맵 대비 소유권이 바뀐 셀: cell -> (old_owner, new_owner)."""
        changed: Dict[str, tuple[str | None, str | None]] = {}
        all_cells = set(self.cell_owner) | set(other.cell_owner)
        for c in all_cells:
            old = other.cell_owner.get(c)
            new = self.cell_owner.get(c)
            if old != new:
                changed[c] = (old, new)
        return changed


def build_shard_map(cells: Sequence[str], nodes: Sequence[str], version: int) -> ShardMap:
    """HRW 로 셀->노드 매핑을 계산해 :class:`ShardMap` 을 만든다."""
    alive = sorted(nodes)
    if not alive:
        return ShardMap(version=version, cell_owner={}, nodes=[])
    cell_owner = {cell: owner_of_cell(cell, alive) for cell in cells}
    return ShardMap(version=version, cell_owner=cell_owner, nodes=alive)


def gini(values: Sequence[float]) -> float:
    """부하 불균형 지표 Gini 계수(0=완전균등, 1=완전편중)."""
    xs = sorted(values)
    n = len(xs)
    if n == 0:
        return 0.0
    total = sum(xs)
    if total == 0:
        return 0.0
    cum = sum((i + 1) * x for i, x in enumerate(xs))
    return (2.0 * cum) / (n * total) - (n + 1.0) / n
