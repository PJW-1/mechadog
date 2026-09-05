# 엔지니어링 가이드 — 로깅 · 테스트 · CI

> 코드를 쓸 때 참조하는 실무 기준. 무엇을 만들지는 [PRD](PRD_Physical_AI_Guard_Robot.md)·[WBS](WBS.md)에, **어떻게 만들지는 이 문서**에 있다.
> 협업 절차는 [CONTRIBUTING](../CONTRIBUTING.md) 참조.

| 항목 | 내용 |
| :--- | :--- |
| **문서 버전** | v1.0.0 |
| **작성일** | 2026-09-03 |
| **대응 요구사항** | NFR-3 (4대 엔지니어링 축) · 아키텍처 4장 (CI/CD) |

---

## 0. 세 가지 원칙

이 문서 전체가 아래 셋으로 요약된다.

| # | 원칙 | 어기면 |
| :-: | :--- | :--- |
| **1** | **로깅은 엣지 트리거 + 주기 요약** | 15fps × 10분 = 9,000줄에 중요한 이벤트가 묻힌다 |
| **2** | **시간과 하드웨어를 주입 가능하게 만든다** | 아무것도 pytest로 검증할 수 없다 |
| **3** | **온보드 로깅 = 텔레메트리** | ESP32에 파일 로깅은 비현실적이고 Wi-Fi 로깅은 부하다 |

---

# 1. 로깅 설계 (NFR-3④)

## 1.1 로그 레코드 형식

**JSON Lines**(`.jsonl`) — 한 줄에 한 레코드.

```json
{"ts":1756800000123,"level":"INFO","device_id":"ref","seq":1234,
 "state":"ALERT","escalation":"L2","track_id":7,"zone":"C",
 "event":"auth_timeout","detail":{"attempts":2,"elapsed_s":31.4}}
```

### 공통 컨텍스트 — 전 레코드 필수

| 필드 | 이유 |
| :--- | :--- |
| `ts` | 여러 노드의 로그를 **시간축에 정렬**하기 위함 |
| `level` | 레벨링 |
| **`device_id`** | **3대 운용이므로 어느 개체의 로그인지 구분 필수** (WBS 8절) |
| `seq` | 명령·텔레메트리와 **상호 참조** |
| `state` | 당시 FSM 상태 |
| `escalation` | 당시 에스컬레이션 단계 |

### 상황별 추가 컨텍스트

| 필드 | 언제 |
| :--- | :--- |
| `track_id` | 사람 관련 이벤트 (FR-3.6) |
| `zone` | 구역 관련 이벤트 (FR-8) |

> **이 표는 규칙이 아니라 검증 대상이다.** 통신 규약과 같은 방식으로 골든 픽스처
> (`tests/fixtures/log_samples.jsonl`)를 두고 `test_logging.py` 가 대조한다 — 컨텍스트가 빠진
> 레코드는 CI 에서 걸린다. 사람이 매번 기억해서 넣는 구조로는 반드시 빠진다 (CONTRIBUTING 5절).
>
> **컨텍스트가 없으면 사후 재구성이 불가능하다.** *"왜 그때 L3로 갔는가"* 를 답할 수 있어야 하고, 그것이 **DR-9(FSM 채택)의 실질적 근거**다.

## 1.2 레벨 정책

| 레벨 | 쓰는 곳 | 예 |
| :--- | :--- | :--- |
| `DEBUG` | 매 프레임·매 명령. **개발 시만** | 추론 시간, 송신 패킷 |
| `INFO` | **상태 변화** | FSM 전이, 에스컬레이션 전이, 인증 결과, 구역 도착, track_id 생성·소멸 |
| `WARN` | 복구 가능한 이상 | 스트림 재연결, 프레임 드롭 임계 초과, **미지 타입 수신**, 저전압 경고 |
| `ERROR` | 기능 상실 | 링크 두절, 페일세이프 진입, VLM 호출 실패 |

기본 레벨은 `INFO` (`config.logging.level`).

## 1.3 ⚠️ 샘플링 — 가장 중요한 제약

```
15 fps × 10분 = 9,000 프레임
```

프레임마다 남기면 **파일이 폭발하고 정작 중요한 이벤트가 묻힌다.**

| 정책 | 방법 |
| :--- | :--- |
| **엣지 트리거** | 값이 **변할 때만** 기록. `PATROL 유지 중`을 9천 번 남기지 않는다 |
| **주기 요약** | fps·지연·검출수·드롭수는 **1초에 한 번 집계**하여 1줄 |
| 카운터 누적 | 순간 이벤트는 카운터로 모아 요약에 싣는다 |

```python
# ❌ 매 프레임
log.info("detected", n=len(dets))

# ✅ 변화 시 + 주기 요약
if state != prev_state:
    log.info("fsm_transition", **{"from": prev_state, "to": state, "trigger": trig})

if now - last_summary >= 1.0:
    log.info("perf_summary", fps=fps, infer_ms=avg_ms, drops=drop_count)
    drop_count = 0
```

## 1.4 노드별 로깅 지점

### Host PC (Python) — 실질적 로깅 주체

| 지점 | WBS | 레벨 | 남길 것 |
| :--- | :--- | :--- | :--- |
| 스트림 수신 | 4.3.3~5 | WARN / 요약 | 연결·재연결, 백오프 단계, **드롭 누적** |
| 추론 | 3.3.2 | DEBUG / 요약 | 추론 시간, 검출 수 |
| 추적기 | 3.3.4 | INFO | **track_id 생성·소멸** |
| **FSM 전이** | 3.4.2 | **INFO** | **전 전이 + 트리거** ← 최우선 |
| **에스컬레이션** | 3.8.3 | **INFO** | 단계 전이 **+ 판단 근거** |
| 명령 송신 | 4.3.2 | DEBUG | seq, 명령 |
| 텔레메트리 수신 | 4.1.4 | 요약 | 배터리, 링크 지연, IMU |
| 인증 | 3.8.1~2 | INFO | 시도·성공·실패 + `track_id` |
| 변화 감지 | 3.6 | INFO | 기준 등록, 변화 확정 |
| 이벤트 블랙박스 | 4.4.3 | — | 스냅샷 JPEG + 텔레메트리 스냅샷 (**로그와 별도 저장**) |
| 클라우드 VLM | 4.8.1 | WARN | 요청·응답·**실패 시 주행 무영향 확인** |

### MechDog ESP32 (C++) — 파일 로깅 안 함

**온보드 로깅은 텔레메트리로 대체한다.**

```json
{"seq":88,"ts":...,"state":"PATROL","dist_cm":47,
 "imu":{"pitch":1.2,"roll":-0.4,"yaw":183.5},"batt_v":7.62,
 "last_cmd_age_ms":34,
 "flags":{"lowbatt":false,"tipped":false,"link_ok":true},
 "events":{"timeout_stops":3,"obstacle_stops":12,"posture_changes":5}}
```

| 규칙 | 이유 |
| :--- | :--- |
| **안전 이벤트는 카운터로 누적** | 10Hz 텔레메트리 사이에 발생한 순간 이벤트를 놓치지 않는다 |
| Serial 출력은 개발 중에만 | 배포 시 최소화 |
| **Wi-Fi 로깅 금지** | 보행 제어 루프에 부담 (DR-6과 같은 논리) |

## 1.5 로테이션

| 설정 | 값 |
| :--- | :--- |
| `logging.rotate_mb` | 10 |
| `logging.rotate_keep` | 5 |
| `logging.dir` | `logs/` |
| `logging.blackbox_dir` | `blackbox/` |

**로그와 블랙박스를 분리한다.** 로그는 로테이션으로 사라지지만, **이벤트 스냅샷은 보존**되어야 한다(FR-3.7).

---

# 2. 테스트 구조

## 2.1 핵심 제약 — 시간과 하드웨어를 주입한다

```python
# ❌ 테스트 불가 — 실제로 300ms 기다려야 하고 결과가 시계에 종속
def check_timeout(self):
    if time.time() - self.last_cmd > 0.3:
        self.hal.move(0, 0)

# ✅ 테스트 가능 — 판정이 순수 함수
def is_command_stale(now_ms: int, last_cmd_ms: int, timeout_ms: int) -> bool:
    return now_ms - last_cmd_ms > timeout_ms
```

**이 원칙 하나가 pytest 가능 범위를 결정한다.**

| 규칙 | 내용 |
| :--- | :--- |
| `time.time()` 직접 호출 금지 | 시각을 인자로 받거나 clock 객체를 주입 |
| HAL 직접 호출 금지 | 인터페이스를 통해 호출 → 목으로 대체 가능 |
| **판정과 실행을 분리** | 판정(순수 함수) → 실행(HAL). 판정만 테스트한다 |

> 이것이 WBS **3.0(판단 로직)과 4.0(소프트웨어)을 나눈 기준**이고, 아키텍처 4.1 원칙 5의 구현 방법이다.

## 2.2 테스트 가능 범위

| 대상 | 가능 | 방법 |
| :--- | :---: | :--- |
| 프로토콜 직렬화·파싱 | ✅ | **골든 픽스처** |
| 필드 범위 클램핑 | ✅ | 경계값 케이스 |
| seq 역전·중복 폐기 | ✅ | 시퀀스 주입 |
| 미지 타입 폐기 | ✅ | `protocol_invalid.jsonl` |
| FSM 전이 | ✅ | **테이블 주도 · 전수** |
| 안전 판정 (타임아웃·저전압·전도) | ✅ | **fake clock** |
| 에스컬레이션 L0~L3 | ✅ | 조건 조합 |
| bbox 클리핑 판정 | ✅ | 좌표만 필요 |
| 추적 ID 연속성 | ✅ | 합성 박스 시퀀스 |
| 변화 감지 비교 | ✅ | 객체 목록만 |
| config 스키마 | ✅ | 이미 구현 |
| 카메라 스트림 | △ | 목업 HTTP 서버 |
| **검출 정확도** | ❌ | 실제 모델·이미지 → **성능 시험(WBS 6.3)** |
| **실제 보행·전도** | ❌ | **실기 검수(WBS 6.4)** |

## 2.3 목표 디렉터리 구조

```
tests/
├── conftest.py                  공통 픽스처 — fake clock · config      ✅
├── fixtures/
│   ├── protocol_samples.jsonl   정본 (8종 + 경계값)      ✅
│   ├── protocol_invalid.jsonl   폐기·클램핑 대상          ✅
│   ├── telemetry_samples.jsonl  상태 13종 + 안전 경계값   ✅
│   ├── telemetry_invalid.jsonl  폐기 대상                 ✅
│   └── log_samples.jsonl        로그 필수 컨텍스트 정본   (4.4.2)
├── test_config.py               config 스키마·불변조건    ✅
├── test_protocol_fixtures.py    픽스처 일관성             ✅
├── test_telemetry_fixtures.py   픽스처 ↔ config 교차검증  ✅
├── test_protocol.py             직렬화·검증 구현          ✅
├── test_mock_mechdog.py         가상 MechDog             ✅
├── test_logging.py              필수 컨텍스트 강제        (4.4.2)
├── test_safety.py               타임아웃·저전압·전도·조합
├── test_fsm.py                  전이표 전수
├── test_escalation.py           L0~L3 진입·해제
├── test_tracker.py              ID 연속성
├── test_change_detect.py        객체 목록 비교
└── test_geometry.py             bbox 클리핑
```

### `conftest.py`의 핵심 부품 — fake clock

```python
@pytest.fixture
def clock():
    """주입 가능한 가짜 시계. 300ms 타임아웃을 0초에 검증한다."""
    class Clock:
        def __init__(self): self.ms = 0
        def advance(self, ms): self.ms += ms
    return Clock()


def test_command_timeout_at_300ms(clock):
    last = clock.ms
    clock.advance(299)
    assert not is_command_stale(clock.ms, last, 300)
    clock.advance(2)
    assert is_command_stale(clock.ms, last, 300)
```

## 2.4 FSM은 테이블 주도로 전수 검증한다

전이표를 **데이터로 표현**하면(WBS 3.4.1) 테스트가 표를 순회하는 것으로 끝난다.

```python
@pytest.mark.parametrize("state,trigger,expected", TRANSITION_TABLE)
def test_every_transition(state, trigger, expected):
    assert fsm.next_state(state, trigger) == expected


def test_undefined_transition_raises():
    """표에 없는 전이는 조용히 무시하지 않고 예외를 낸다."""
    with pytest.raises(UndefinedTransition):
        fsm.next_state("PATROL", "NONSENSE")
```

## 2.5 C++ 파서도 같은 픽스처로 닫는다

`command_parser.*` 를 **HAL 비의존**으로 작성하는 이유가 여기 있다.

```
tests/fixtures/protocol_samples.jsonl
      ├─▶ Python 직렬화 검증        pytest
      └─▶ C++ 파서 검증             리눅스 CI에서 호스트 컴파일 (ESP32 불필요)
```

작은 테스트 하네스(`main()`)가 픽스처를 읽어 파싱 결과를 출력하고, 기대값과 대조한다. **양쪽이 같은 정본을 보므로 불일치가 원리적으로 막힌다.**

---

# 3. CI 게이트

파이프라인 구성은 [아키텍처 4절](ARCHITECTURE.md), PR 전 로컬 실행은
[CONTRIBUTING 8절](../CONTRIBUTING.md)에 있다. 여기서는 **왜 그 게이트인지**만 적는다.

### 커버리지 하한 70% — 실측 100% 인데 왜 70 인가

```yaml
run: pytest -q --cov=host --cov=tools --cov-report=term-missing --cov-fail-under=70
```

- **`--cov-fail-under` 가 없으면 커버리지는 게이트가 아니라 장식이다.** 숫자만 찍히고 아무도 안 본다.
- **하한을 실측치까지 올리지 않는다.** 그러면 커버리지를 떨어뜨리지 않는 것 자체가 목적이 되어
  **의미 없는 테스트를 쓰게 된다.** 70% 는 *"핵심 경로가 검증되지 않은 채 머지되는 것"* 을 막는 하한이다.
- **`tools/` 를 함께 센다.** 가상 MechDog(`mock_mechdog.py`)은 시험 도구가 아니라 **시험 대상**이다 —
  목업이 틀리면 그것으로 검증한 호스트 코드가 전부 헛것이 된다.
- 첫 Host 모듈이 들어오기 전까지는 **일부러 꺼 두었다.** 잴 코드가 없으면 `No data was collected`
  경고만 남고 CI 는 통과한다 — 아무것도 검증하지 않으면서 통과하는 상태를 만들지 않기 위해서다.

### 앞으로 추가할 것

| 시점 | 추가 | WBS |
| :--- | :--- | :--- |
| M1 | **C++ 파서 픽스처 대조 잡** (호스트 컴파일) | 6.2.1 |
| M2 | 성능 회귀 감시 (선택) | 6.3.1 |

---

# 4. 환경은 통일하지 않는다

팀은 **각자의 GPU · OS 환경에서 개발한다.** 동일 환경 재현은 요구사항이 아니며,
컨테이너로 호스트 스택을 묶지도 않는다 (DR-17).

## 통일하는 것은 셋뿐이다

| | 왜 |
| :--- | :--- |
| **메시지 스키마** | 송·수신을 다른 사람이 만든다 → [PROTOCOL.md](PROTOCOL.md) |
| **`.onnx` 산출물** | 누가 만들든 나머지가 그대로 쓴다 |
| **성능 수치의 출처** | 보고서 숫자는 기준 PC 실측 한 벌만 (WBS 8절) |

세 번째는 환경 통일이 아니라 **측정 기준 고정**이다. PC 가 달라도 되고, NFR 수치만
기준 PC 에서 뽑아 문서화하고 나머지는 참고치로 병기한다.

## 그래서 실행 프로바이더는 교집합으로 고른다

`config.vision.providers` 는 **선호 순서**이지 요구사항이 아니다. 설치된 패키지에 따라
사용 가능한 EP 가 다르므로, 반드시 실제 가용 목록과 교집합을 취한다.

```python
# ❌ 설정을 그대로 넘기면 없는 EP 에 대해 경고가 쏟아진다
session = ort.InferenceSession(path, providers=cfg["vision"]["providers"])

# ✅ 설치된 것만 남긴다
available = ort.get_available_providers()
providers = [p for p in cfg["vision"]["providers"] if p in available]
if not providers:
    raise RuntimeError(f"사용 가능한 EP 없음. 설치됨={available}")
session = ort.InferenceSession(path, providers=providers)
log.info("execution_provider", extra={"selected": providers[0], "available": available})
```

| 그 사람이 설치한 패키지 | 선택되는 EP |
| :--- | :--- |
| `onnxruntime-directml` (기본) | DirectML — DX12 GPU 전체. NVIDIA 포함 |
| `onnxruntime-gpu` | CUDA |
| `onnxruntime` (CI 러너) | CPU |

**같은 코드, 같은 모델 파일, 다른 환경.** 이것이 DR-13 이 TensorRT 를 배제한 이유이기도
하다 — TensorRT 엔진은 GPU 아키텍처마다 다시 빌드해야 해서 이 구조가 성립하지 않는다.

> 선택된 EP 를 **기동 시 반드시 로그로 남긴다.** DirectML 은 미지원 연산을 조용히 CPU 로
> 내려보내므로, 어느 EP 로 돌고 있는지 기록이 없으면 성능 이상의 원인을 찾을 수 없다.

---

---

> **4대 엔지니어링 축의 구현 지침과 "넘어서는 안 되는 선"은
> [CONTRIBUTING 7절](../CONTRIBUTING.md)에 있다.** 코드 리뷰 기준이므로 협업 규칙 쪽이 정본이다.
