"""데모: 분산 클러스터를 배치로 돌려 합승 효과 + 분산 동작(장애→복구)을 텍스트로 확인.

대시보드 없이 빠르게 동작을 보고 싶을 때:
    python examples/run_demo.py
"""

from drt_sim.config import SimConfig
from drt_sim.experiments import compare_baseline, run_once


def main() -> None:
    print("=" * 64)
    print(" DRT 분산 합승 배차 — 합승 vs 비합승 (동일 수요·차량)")
    print("=" * 64)
    cfg = SimConfig.from_yaml("config/default.yaml")
    cfg.duration = 1200.0
    for res in compare_baseline(cfg):
        print(res.render(), "\n")

    print("=" * 64)
    print(" 분산 동작 — 워커 장애 → 샤드 리밸런싱 → 복구")
    print("=" * 64)
    fault_cfg = SimConfig.from_yaml("scenarios/node_failure.yaml")
    res = run_once(fault_cfg, "node_failure 시나리오")
    print(res.render())
    print("\n→ 복구시간(time-to-recovery)이 기록되고, 죽은 노드의 차량은 무중단으로 "
          "생존 노드에 재할당된다(이중 배차 차단 0).")


if __name__ == "__main__":
    main()
