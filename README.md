# drt-dispatch-sim

**실시간 수요응답형(DRT) 합승 배차 엔진 시뮬레이터 — 분산 시스템 중심**

현대 셔클(SHUCLE) 류 DRT 서비스의 배차 엔진을 모사하되, 이 프로젝트의 주인공은
배차 알고리즘이 아니라 **그것을 떠받치는 분산 시스템 설계**다. 지오 샤딩, 노드 간
조율, 리더 선출, 장애 복구, 백프레셔, 멱등 전달을 **결정론적 가상시간 시뮬레이터**
위에 구현하고, 코드를 모르는 사람도 30초 만에 동작을 이해할 수 있는 **Plotly Dash
대시보드**로 시각화한다.

> 설계 우선순위: (a) 올바른 분산 조율(이중 배차 없음, 깔끔한 페일오버) → (b) 직관적
> 시각화 → (c) 깔끔한 async Python → (d) 재현성 → (e) 명확한 트레이드오프 문서화.
> 라우팅 최적성에는 과투자하지 않는다(견고한 휴리스틱 수준).

---

## 30초 데모

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
python -m drt_sim.cli dashboard --config config/default.yaml
# 브라우저에서 http://127.0.0.1:8050
```

대시보드에서 **▶ 워커 장애 / 리더 장애 / 수요 폭증 / 파티션 / 워커 추가** 버튼을
누르면, 지도에서 샤드가 다시 칠해지고(마이그레이션) 토폴로지에서 리더 배지가 옮겨가며
큐가 차올랐다 회복되는 과정을 실시간으로 볼 수 있다.

| 정상 운영 | 워커 장애 → 샤드 리밸런싱 |
|---|---|
| 소유 노드별 색으로 칠해진 H3 지오 샤드 + 실시간 차량/경로 | 죽은 노드(회색 점선)의 셀이 생존 노드로 재분배, 복구시간 표시 |

---

## 무엇이 보이나 (대시보드)

- **지도 패널** — 세종시 실제 도로 타일맵 위에 운영지역을 덮는 **H3 셀을 소유 노드별 색**으로 표시.
  차량은 소유 노드색 점(탑승 인원에 따라 크기), 경로는 선, 유휴 리밸런싱 선이동은 파란
  점선. **노드가 죽으면 그 셀이 생존 노드 색으로 다시 칠해지며 샤드 마이그레이션이 보인다.**
- **클러스터 토폴로지** (`dash-cytoscape`) — 코디네이터 + 워커 그래프. **★ 리더 배지**,
  과부하(빨강 테두리)/장애(회색 점선) 헬스, 노드별 큐 깊이·누적 처리량, 코디네이터 간
  합의 엣지(점선).
- **차트** — 배차 지연 p50/p95/p99, 노드별 큐 깊이/처리량, 큐 추이(백프레셔).
- **트레이스 로그** — 요청 하나가 `gateway → bus → worker → store` 를 거치는 분산
  트레이스 스트림.
- **컨트롤** — 노드/리더 장애, 네트워크 파티션, 수요 폭증, 워커 추가, 재생/일시정지/배속.

---

## 아키텍처

```
                         ┌──────────────────────────┐
                         │   Coordinator (control)  │   lease 기반 리더 선출
        heartbeat ─────► │  coord-0 ★   coord-1     │   멤버십·헬스·리밸런싱
        ┌────────────────┤  - HRW 샤드맵 계산        │   클러스터 뷰 발행
        │                │  - 장애 감지 → 재할당     │   백프레셔 spill 흡수
        │   cluster_state│  - spill 재주입          │
        │   shard_events └─────────┬────────────────┘
        │                          │ cluster_state (shard map, leader, health)
        │                          ▼
   ┌────┴─────┐   ride_req   ┌──────────────────────────────────┐
   │ Gateway  │─────────────►│  Dispatch Workers (data plane)    │
   │ 수요발생 │  (소유 워커로 │  worker-0  worker-1  worker-2 ... │
   │ +라우팅  │   직접 라우팅)│  - 자기 샤드 요청 매칭(insertion) │
   └──────────┘              │  - 백프레셔(틱당 처리량 한계)     │
        ▲                    │  - 멱등 dedupe / 크로스샤드 handoff│
        │ cluster_state      └──────────┬────────────────────────┘
        │                               │ commit_assignment (낙관적 동시성)
   ┌────┴──────────────────┐            ▼
   │  Message Bus           │   ┌──────────────────────────┐
   │  - SimBus(인메모리,     │   │  Vehicle State Store      │
   │    지연·유실·재정렬·    │   │  - 차량별 단일 라이터      │
   │    파티션·중복 모델링)  │   │  - version 낙관적 동시성   │  ← 이중 배차 차단
   │  - RedisBus(Streams)   │   │  - 팔로워 복제(장애 복구)  │
   └────────────────────────┘   └──────────────────────────┘
                  ▲
   ┌──────────────┴───────┐
   │  World (물리/데이터)  │  차량 이동·픽업/하차·텔레메트리 (제어 플레인과 분리)
   └──────────────────────┘

   ── 전부 ──►  Runtime (결정론적 가상시간 협조 스케줄러)  ◄── 시드 RNG
```

모든 컴포넌트는 진짜 `async def` actor 이며, 하나의 **가상시간 런타임** 위에서 돈다.
actor 는 `await ctx.sleep(dt)` 로 시간을 진전시키고 `await ctx.recv()` 로 메시지를
기다린다. wall-clock 블로킹 호출이 없다.

### 모듈 구조

```
src/drt_sim/
  sim_clock.py     # ★ 결정론적 가상시간 async 런타임(백본)
  async_runtime.py # wall-clock asyncio 런타임(가상시간 런타임의 drop-in 대체)
  bus/             # 메시지 버스: base(인터페이스) + sim_bus + redis_bus
  sharding.py      # H3 셀 → 노드 매핑(HRW rendezvous 해싱), Gini
  store.py         # 차량 상태 스토어(단일 라이터 + 낙관적 동시성 + 복제)
  coordinator.py   # 리더 선출(lease)·멤버십·리밸런싱·백프레셔
  worker.py        # 지오 샤드 소유·매칭·멱등·핸드오프·spill
  gateway.py       # 수요 발생 + 소유권 기반 라우팅
  world.py         # 차량 물리(이동·픽업/하차)
  dispatch/        # insertion(합승) + virtual_stop(가상정류장) + baseline(비합승)
  geo.py           # 평면 km 좌표 + 위경도 투영 + H3
  routing.py       # 도로망 라우터(StraightRouter / OSMRouter) — 이동 형상
  demand.py        # 시변 포아송 + 핫스팟 수요 생성기
  protocol.py      # 노드 간 메시지 페이로드
  config.py        # pydantic 설정 + YAML 로더
  live_metrics.py  # 분산 메트릭(지연 p50/95/99, throughput, 복구시간)
  metrics.py       # 서비스 품질 지표
  tracing.py       # 분산 트레이싱(trace_id 스팬)
  cluster.py       # 클러스터 조립 + 시뮬레이션 컨트롤러 + 스냅샷
  live_cluster.py  # live(asyncio) 모드 실행
  cluster_redis.py # 진짜 분산: 실제 OS 프로세스 + Redis lease/스트림 조율
  viz/             # Plotly Dash 대시보드(runner/figures/app)
  cli.py           # CLI 진입점
config/ scenarios/ # YAML 설정·장애 시나리오
tests/             # 분산 속성 검증(pytest)
```

---

## 분산 시스템 설계 결정과 트레이드오프 (왜 이렇게 했는가)

### 1. 결정론적 가상시간 런타임 (vs 실제 asyncio + wall-clock)
실제 `asyncio` 이벤트 루프는 wall-clock 에 묶여 **재현 불가능**하고 디버깅이 어렵다.
대신 `sim_clock.py` 에 **커스텀 awaitable 로 구동되는 협조적 가상시간 스케줄러**를
직접 만들었다. actor 는 여전히 진짜 `async def` 이지만, 모든 대기가 `(time, seq)` 로
전순서가 매겨진 이벤트 힙으로 변환된다.
- **장점**: 시드만 같으면 트레이스까지 완전히 동일(테스트로 보장). 단일 스레드라
  동시성 버그 없음. 메시지 지연/재정렬/유실을 시간 이벤트로 정밀 모델링.
- **트레이드오프**: 진짜 멀티코어 병렬성은 없다. "진짜 분산"이 필요하면 `RedisBus` +
  multiprocessing 의 **cluster 모드**로 각 노드를 별도 OS 프로세스로 띄운다(아래).
- **이식성 증명**: `AsyncioRuntime` 이 같은 표면을 wall-clock asyncio 로 구현하므로,
  **같은 actor 코드·같은 버스**가 가상시간과 진짜 asyncio 양쪽에서 수정 없이 돈다
  (`cluster --mode live`). actor 가 런타임에 결합돼 있지 않다는 증거다.

### 2. HRW(rendezvous) 해싱 (vs consistent hashing 링)
셀→노드 매핑을 `hash(cell, node)` 최대값으로 정한다.
- **장점**: 노드 추가/제거 시 **평균 1/N 셀만 이동**(테스트로 검증) → 페일오버 시
  마이그레이션 최소화 + 지도에서 "일부 셀만 다시 칠해짐". 가상노드 링 관리 불필요,
  노드 집합만 같으면 모두 동일 결론.
- **트레이드오프**: 완벽한 균형은 아니다(소수 노드에서 Gini 편차). 셀 수를 늘리거나
  (H3 해상도↑) 부하 가중 보정으로 개선 가능.

### 3. 차량별 단일 라이터 + 낙관적 동시성 (이중 배차 방지의 핵심)
모든 차량은 정확히 한 노드가 소유하고, 배차는 `commit_assignment(veh, expected_version,
…, by_node)` 로만 확정된다. 소유자 불일치 또는 버전 불일치면 거부된다.
- **왜**: 두 워커가 같은 차량을 동시에 노려도 한쪽만 성공 → **이중 배차 불가**. 샤드
  마이그레이션 시 버전을 올리므로 옛 소유자의 in-flight 쓰기가 자동 무효화된다.
- **트레이드오프**: 차량 소유권을 **home 셀**에 고정(이동해도 불변)했다. 덕분에 페일오버
  의미가 깔끔하지만, 차량이 멀리 이동하면 해당 샤드의 가용 차량이 줄어 매칭이 약해질
  수 있다(유휴 차량 리밸런싱은 향후 과제).

### 4. 리더 선출: lease 기반 (vs 완전한 Raft)
코디네이터 후보들이 공유 lease(개념적으로 Redis SETNX+TTL)를 두고 경쟁한다. 리더만
lease 를 갱신하고 제어 작업을 수행하며, 죽으면 lease 만료 후 다른 후보가 승격한다.
- **장점**: 단순하고 시연이 명확하다(리더 죽이면 수 초 내 새 리더). 후보 전원이 헬스를
  추적하므로 페일오버가 매끄럽다.
- **트레이드오프**: 완전한 로그 복제 합의(Raft)는 아니다. 클러스터 상태는 멤버십
  하트비트에서 재구성하므로 split-brain 방지는 lease TTL 에 의존한다.

### 5. 결과적 일관성 + 스테일 대응
클러스터 뷰(`ClusterView`)는 **버전**을 달고 전파된다. 전파 지연 때문에 게이트웨이가
스테일 뷰로 옛 소유자에게 요청을 보낼 수 있는데, 받은 워커가 현재 소유자로 **재라우팅**
한다. 크로스 샤드(픽업≠하차 샤드) 요청은 하차 샤드 노드에 **handoff** 인지 이벤트를
보낸다.

### 6. 백프레셔 (틱당 처리량 한계 + spill)
워커는 틱당 처리량이 제한되어 수요 폭증 시 큐가 쌓인다. 고수위를 넘으면 초과분을
코디네이터로 **spill** 하고, 코디네이터가 매 틱 제한된 수만큼 소유 워커로 재주입해
폭증을 점진적으로 흡수한다(graceful degradation).

### 7. at-least-once + 멱등성
`SimBus` 는 확률적으로 메시지를 **중복 전달**한다(`dup_prob`). 워커는 `request_id` 로
dedupe 하여 같은 요청을 두 번 배차하지 않는다.

### 8. 유휴 차량 리밸런싱: 미충족 수요 신호 기반 (반응형)
워커는 **거절(미충족 수요)**이 난 셀을 공급 부족 신호로 보고, 자기 샤드의 유휴 차량을
그 셀로 선이동(deadhead)시킨다. 각 워커가 자기 샤드 안에서만 수행하므로 전역 조율 없이
분산적이다.
- **왜 거절 신호인가**: 매칭이 충분하면(거절 0) 아무 차량도 움직이지 않아 deadhead
  오버헤드가 0이다. 공급이 부족할 때만 작동한다.
- **검증**: 충분 공급(36대)에서는 결과가 켜짐/꺼짐 동일(무동작), 공급 부족(16대·5워커
  단편화)에서는 여러 시드 평균 매칭률↑·거절↓. VKT 는 선이동만큼 증가하는 트레이드오프.

### 9. 도로망 라우팅: 형상만 도로, 시간은 유클리드
차량이 정류점 사이를 직선으로 가로지르면 실제 지도 위에서 어색하다. 그래서 **차량의
이동 경로 형상은 실제 OSM 도로(OSMnx 최단경로)를 따라간다**. 단, 도착 시각은 엔진이
가정한 **유클리드 통행시간**에 맞춘다(폴리라인 호 길이 기준 보간).
- **왜 이렇게**: 스펙이 "라우팅 최적성에 과투자 금지"라 했다. 배차 엔진은 빠른·결정론적
  유클리드 통행시간을 유지하고, 시각화 형상만 도로를 따르게 분리했다. `StraightRouter`
  면 직선 모드와 **바이트 단위로 동일**(결정성 테스트 통과).
- **트레이드오프**: 도로 라우팅은 시각화용이라 배차 의사결정엔 반영되지 않는다. OSM
  통행시간을 엔진에 넣으면 더 정확하지만 인서션 후보마다 최단경로 계산 → 느리고
  비결정론적이라 의도적으로 분리했다. `routing.backend: straight` 로 끄면 의존성 0.
- **폴백**: osmnx 미설치/그래프 없으면 자동으로 직선. 도로 그래프는 `data/` 에 동봉,
  다른 운영지역은 `area.center_lat/lon` 변경 후 `build-graph` 로 재생성.

### 10. 지도 렌더링: 실제 타일맵 기본 + SVG 폴백
기본은 **실제 도로 타일맵(`go.Scattermap`, MapLibre, 토큰 불필요)** 위에 H3 샤드·차량·
경로를 얹는다. 세종시 실제 지리 위에서 지오 샤딩이 보여 평가자에게 가장 직관적이다.
- **폴백**: `config.area.map_style: svg` 로 두면 외부 타일·WebGL 없이 평면 km **SVG
  벌집**으로 그린다. 오프라인·저사양·CI 스크린샷 환경에서도 항상 렌더된다.
- 두 렌더러는 같은 스냅샷(km 좌표)을 쓰고, 타일맵은 km 를 위경도로 역투영해 표시한다.

### 11. 합승 균형: 운영자 목적 + 전(全) 승객 보호 제약 (다목적)
합승은 **승객(직선에 가깝게)과 운영자(많이 태워 VKT↓)의 이해가 충돌**하는 다목적
문제다. 단일 최적점이 아니라 **파레토 곡선**이라, "목적 + 제약"으로 분리해 푼다.
- **목적함수 = 운영자 효율**: 삽입 시 추가 차량 주행시간(한계비용) 최소 → 합승 선호.
- **제약 = 승객 보호**: `max_wait`(대기 상한) + `max_detour_factor`(우회 상한) + 정원.
  `_feasible` 은 **경로 위 모든 승객**(새 요청뿐 아니라 이미 탔거나 배차된 승객까지)의
  대기·우회를 재검증한다 — 새 손님을 끼우려고 **기존 손님을 한도 넘게 돌리지 못한다**
  (이미 탑승한 승객은 `boarded_at`, 미탑승 승객은 경로상 픽업 시각으로 우회 계산).
- **균형점 선택 = 파레토 스윕**: `cli pareto` 로 `max_detour_factor` 를 1.1→2.0 스윕하면
  "허용 한도↑ → 매칭률·합승률↑, VKT↓, 단 승객 우회↑" 곡선이 나온다. **발견**: 한도를
  풀어도 *실제* 평균 우회는 1.0~1.1x 에 머문다 — 한도는 합승 *성사*를 가능케 하는 빗장일
  뿐, 대부분의 합승은 거의 안 돌아간다. 보합점(knee)은 ~1.5x.

---

## 실행법

```bash
# 설치
python -m venv .venv && source .venv/bin/activate
pip install -e .            # 전체: pip install -e ".[redis,osm,dev]"
# 실제 도로망 라우팅을 쓰려면 [osm] 설치 후(미설치 시 자동으로 직선 폴백):
#   pip install -e ".[osm]"
#   python -m drt_sim.cli build-graph   # 운영지역 도로 그래프 캐시(저장본 동봉, 재생성용)

# 1) 대시보드 (단일 커맨드 데모)
python -m drt_sim.cli dashboard --config config/default.yaml
python -m drt_sim.cli dashboard --scenario scenarios/node_failure.yaml

# 2) 배치 실행 + 메트릭
python -m drt_sim.cli run --scenario scenarios/demand_surge.yaml

# 3) 합승 vs 비합승 비교
python -m drt_sim.cli baseline --config config/default.yaml --duration 1200

# 4) 확장성 실험(워커 수)
python -m drt_sim.cli scaling --config config/default.yaml --workers 1,2,4,6

# 4b) 승객 우회 ↔ 운영 효율 파레토 곡선(허용 우회 한도 스윕)
python -m drt_sim.cli pareto --config config/default.yaml --duration 1200 \
    --factors 1.1,1.3,1.5,1.7,2.0 --html pareto.html

# 5) 진짜 분산 — live(asyncio) 모드: 같은 actor 가 실제 asyncio 루프에서 실행(redis 불필요)
python -m drt_sim.cli cluster --mode live --scale 20 --virtual-seconds 60

# 6) 진짜 분산 — redis 멀티프로세스 모드: 각 노드를 실제 OS 프로세스로 기동(redis-server 필요)
#    제어 플레인(하트비트·리더 선출·장애 감지)이 Redis Streams 로 프로세스 간 조율된다.
pip install -e ".[redis]" && redis-server &   # 별도 터미널
python -m drt_sim.cli cluster --mode redis --workers 3 --kill-at 4

# 테스트 (redis 없이도 전부 실행 — fakeredis 공유 서버로 분산 로직 검증)
pytest -q
```

### 시나리오 (`scenarios/`)
- `node_failure.yaml` — 워커 장애 → 샤드 리밸런싱 → 복구 → 워커 추가
- `demand_surge.yaml` — 수요 6배 폭증 → 큐 증가(백프레셔) → 회복
- `leader_failover.yaml` — 코디네이터(리더) 장애 → 리더 재선출
- `network_partition.yaml` — 네트워크 파티션 → 격리 노드 장애 인지 → 해제 후 재가입

설정은 모두 YAML 주도이며 `seed` 로 완전 재현된다.

---

## 결과 (예시, `baseline --duration 1200`)

| | 합승(insertion) | 비합승(baseline) |
|---|---|---|
| 매칭률 | **99%** | 47% |
| 합승률 | **80%** | 0% |
| 총주행거리(VKT) | **115 km** | 183 km |
| 평균 대기 | 169 s | 292 s |
| 배차 지연 p99 | 0.57 s | 0.56 s |

→ 같은 차량 수로 합승은 **2배 더 태우고**(98 vs 47 배차) **VKT 를 37% 절감**한다.

**분산 지표 (node_failure 시나리오)**
- 워커 장애 후 **time-to-recovery ≈ 8s**(= 하트비트 타임아웃 + 리밸런싱)
- 장애 시 **HRW 로 죽은 노드의 셀만 이동**, 차량 소유권 무중단 재할당
- 배차 지연 p99 가 워커 수와 무관하게 sub-second 유지 → 제어 플레인 수평 확장성
- 이중 배차 차단 0건(정상), 경쟁 시도는 스토어 거부 카운터로 가시화

---

## 진짜 분산(cluster) 모드 — 별도 OS 프로세스

기본 sim 모드는 단일 프로세스·결정론이다. 아키텍처가 "진짜 분산"임을 두 단계로 보인다.

**(1) live(asyncio) 모드** — `AsyncioRuntime` 은 가상시간 `Runtime` 의 drop-in 대체로,
같은 표면(`now`/`schedule_at`/`deliver`/`spawn`)을 진짜 asyncio 이벤트 루프로 구현한다.
**동일한 actor 코드와 동일한 `SimBus` 가 수정 없이** wall-clock asyncio 위에서 돈다.
actor 가 특정 런타임에 묶여 있지 않음을 증명한다(redis 불필요, 어디서나 실행).

```bash
python -m drt_sim.cli cluster --mode live --scale 20 --virtual-seconds 60
```

**(2) redis 멀티프로세스 모드** — 각 코디네이터/워커를 `multiprocessing` 으로 **실제
별도 OS 프로세스**로 띄우고, **Redis Streams 하트비트 + Redis 키 lease 리더 선출**로
제어 플레인을 프로세스 간에 조율한다. 중간에 한 워커 프로세스를 종료하면 리더 코디네이터가
하트비트 단절로 장애를 감지해 클러스터 뷰를 갱신한다(분산 시스템의 가장 어려운 부분 —
리더 선출·장애 감지 — 이 진짜 프로세스 경계를 넘어 동작). 데이터 플레인(차량 스토어·물리)은
결정론을 위해 sim 모드에 둔다.

```bash
redis-server &                                   # 별도 터미널
python -m drt_sim.cli cluster --mode redis --workers 3 --kill-at 4
```

> redis-server 가 없어도 이 로직은 테스트된다: `tests/test_redis_cluster.py` 가 **공유
> fakeredis 서버 + 스레드**로 여러 노드를 한 프로세스에 띄워 lease 리더 선출·하트비트
> 스트림·장애 감지를 검증한다. 실제 실행은 같은 노드 루프를 프로세스 + redis-server 로 돌린다.

---

## 테스트 (`pytest`)

- `test_no_double_assign.py` — 단일 라이터·낙관적 동시성이 경쟁/스테일 쓰기를 차단
- `test_sharding.py` — HRW 결정성 + 노드 추가/제거 시 최소 이동
- `test_cluster.py` — 결정성(시드 동일→트레이스 동일), 멱등성(중복 전달), 리밸런싱,
  리더 페일오버, 복구시간 기록, **정원 불변**, 유휴 리밸런싱(부족 시 작동·충분 시 무동작)
- `test_async_runtime.py` — AsyncioRuntime 동작 + **live 클러스터가 진짜 asyncio 에서 실행**
- `test_redis_cluster.py` — Redis lease 리더 선출·만료 페일오버, Streams 라운드트립,
  **프로세스 간 장애 감지**(공유 fakeredis + 스레드)
- `test_engine.py` — 배차 제약(정원·대기·픽업선행), **전 승객 우회 보호**(삽입이 기존/
  탑승 승객을 한도 넘게 돌리면 거부)

총 33개 테스트, redis-server 없이 모두 실행된다.

---

## 한계 & 다음 단계

- **배차 통행시간은 직선거리 근사** — 차량 이동 *형상*은 실제 OSM 도로를 따르지만(`routing.py`),
  배차 *의사결정*의 통행시간은 결정론·속도를 위해 유클리드를 유지한다(의도적). OSM/OSRM
  통행시간을 엔진에 넣는 것이 다음 단계(인서션 후보별 최단경로 캐싱 필요).
- **리더 선출은 lease 단순화** — sim 모드는 인메모리 lease, redis 모드는 Redis 키 lease.
  강한 일관성이 필요하면 Raft 로그 복제로 확장.
- **redis 모드의 데이터 플레인** — 현재 redis 멀티프로세스 모드는 제어 플레인(리더 선출·
  멤버십·장애 감지)을 실제 프로세스 간에 조율한다. 데이터 플레인(차량 스토어·배차)까지
  프로세스 분산하려면 Redis 백엔드 스토어 + 타입드 페이로드 (역)직렬화 레이어가 필요하다.
- **유휴 리밸런싱은 반응형** — 현재는 거절(미충족 수요) 신호 기반. 시계열 수요 예측(ML)
  으로 선제적 선이동으로 확장 가능.
