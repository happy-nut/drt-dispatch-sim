"""지오 샤딩(HRW) 정확성 + 최소 이동 검증."""

from drt_sim.geo import GeoProjection
from drt_sim.sharding import build_shard_map, gini


def _cells():
    proj = GeoProjection(36.48, 127.28, 10.0, 7)
    return proj.cells_covering_area()


def test_every_cell_owned_by_alive_node():
    cells = _cells()
    nodes = ["worker-0", "worker-1", "worker-2"]
    sm = build_shard_map(cells, nodes, version=1)
    assert all(sm.owner(c) in nodes for c in cells)
    assert set(sm.nodes) == set(nodes)


def test_hrw_deterministic():
    cells = _cells()
    nodes = ["w-a", "w-b", "w-c"]
    a = build_shard_map(cells, nodes, version=1)
    b = build_shard_map(cells, list(reversed(nodes)), version=1)
    assert a.cell_owner == b.cell_owner  # 노드 순서 무관


def test_node_removal_moves_only_its_cells():
    """노드 제거 시 그 노드가 갖던 셀만 이동(HRW 최소 이동성)."""
    cells = _cells()
    full = build_shard_map(cells, ["w0", "w1", "w2"], version=1)
    reduced = build_shard_map(cells, ["w0", "w1"], version=2)
    w2_cells = set(full.cells_of("w2"))
    for c in cells:
        if full.owner(c) != reduced.owner(c):
            assert c in w2_cells  # 옮겨간 셀은 전부 w2 소유였던 것


def test_node_addition_only_pulls_cells():
    """노드 추가 시 새 노드는 기존 노드들에서 일부만 가져온다(다른 셀 불변)."""
    cells = _cells()
    before = build_shard_map(cells, ["w0", "w1"], version=1)
    after = build_shard_map(cells, ["w0", "w1", "w2"], version=2)
    for c in cells:
        if after.owner(c) != before.owner(c):
            assert after.owner(c) == "w2"  # 바뀐 셀은 전부 새 노드로


def test_gini_bounds():
    assert gini([5, 5, 5, 5]) == 0.0
    assert 0.0 < gini([0, 0, 0, 20]) <= 1.0
