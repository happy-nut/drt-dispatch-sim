# drt-dispatch-sim

실시간 **수요응답형(DRT, Demand-Responsive Transport) 합승 배차 엔진** 시뮬레이터 (PoC).

라이더의 호출 요청이 실시간 스트림으로 들어올 때, 여러 대의 차량에 합승(ride-pooling)
경로를 동적으로 삽입(insertion)하여 배차하는 엔진을 이산 시간(discrete-time)
시뮬레이션 위에서 검증한다.

## 무엇을 다루나

- **수요 생성**: 시공간 분포 기반의 호출 요청 스트림 생성 (`demand.py`)
- **배차 엔진**: greedy insertion heuristic — 각 신규 요청을 모든 차량의 현재 경로에
  삽입해 보고, 제약(정원, 픽업/하차 시간 윈도우, 우회 한도)을 만족하면서
  추가 비용이 최소인 위치에 배정 (`engine.py`)
- **시뮬레이터**: 시간 전진, 차량 이동, 요청 도착/매칭/완료 이벤트 처리 (`simulator.py`)
- **지표**: 매칭률, 합승률, 평균 대기/우회 시간, 차량 가동률 (`metrics.py`)

## 빠른 실행

```bash
pip install -e .
python -m drt_sim.cli --vehicles 10 --duration 3600 --seed 42
# 또는
python examples/run_demo.py
```

## 구조

```
src/drt_sim/
  models.py      # Request, Vehicle, Stop, RouteStop 등 도메인 모델
  geo.py         # 좌표/거리/이동시간 (격자 + haversine)
  demand.py      # 수요(호출) 스트림 생성기
  engine.py      # insertion 기반 합승 배차 엔진
  simulator.py   # 이산 시간 시뮬레이션 루프
  metrics.py     # 성능 지표 집계
  cli.py         # 시나리오 실행 진입점
tests/           # 엔진/제약 검증 테스트
examples/        # 데모 시나리오
```

## 상태

PoC. 알고리즘과 제약 모델을 빠르게 실험하기 위한 골격이며,
실시간 재최적화·rebalancing·ML 수요예측 등은 향후 확장 지점으로 남겨둠.
