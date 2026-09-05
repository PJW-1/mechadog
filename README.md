# MechDog Physical AI — 경비 · 산업안전 점검 4족 로봇

Hiwonder MechDog(ESP32)과 Seeed XIAO ESP32S3 Sense, Host PC를 결합한 **자율 순찰 4족 보행 로봇** 프로젝트입니다.

로봇은 한 번의 순찰에서 **경비**(침입자 인지·인증·경보)와 **산업안전 점검**(보호구 미착용 감지)을 **동시에** 수행합니다. 같은 카메라 프레임에서 두 판정이 함께 나옵니다.

MechDog이 제공하는 오픈소스 모션 라이브러리(`HW_MechDog`)를 **HAL로 취급**하고, 그 위에 **인지 → 판단 → 항법** 자율 스택을 새로 얹는 것이 목표입니다.

📄 **[PRD v1.0 — 요구사항 및 설계 결정](docs/PRD_Physical_AI_Guard_Robot.md)**  
📋 **[WBS — 작업 분해 구조 및 일정](docs/WBS.md)**  
🙋 **[담당자별 작업 목록 — 내 일이 뭔지](docs/ASSIGNMENTS.md)** ← 자기 할 일  
🧭 **[설계 결정 기록 — 무엇을 왜 안 했나](docs/DECISIONS.md)** ← ADR 18건  
🔧 **[M0 하드웨어 검증 체크리스트](docs/HARDWARE_VERIFICATION.md)**  
🔌 **[하드웨어 조립·배선 — 무납땜 구성](docs/HARDWARE_ASSEMBLY.md)**  
🛠️ **[엔지니어링 가이드 — 로깅·테스트·CI](docs/ENGINEERING_GUIDE.md)**  
📡 **[통신 프로토콜 정본 — 명령 8종·검증 규칙](docs/PROTOCOL.md)**  
🏗 **[시스템 아키텍처 — 구조·상태·품질 기준](docs/ARCHITECTURE.md)** ← 용어 부록 포함  
🤝 **[협업 규칙](CONTRIBUTING.md)**

> **처음 클론했다면** — `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1` (약 40초)

---

## 아키텍처 — 스타 토폴로지

모든 노드는 독립 전원을 가지며, **로봇 위의 노드끼리는 배선하지 않습니다.** Host PC가 허브입니다.

```
                  ┌──────────────────────────────┐
                  │       HOST PC (허브)          │
                  │  객체 검출 · FSM · 대시보드   │
                  └──┬──────────────┬────────────┘
        MJPEG ↑      │              │ UDP 명령 ↓ / 텔레메트리 ↑
        ┌────────────┴───────┐  ┌───┴──────────────────┐
        │ XIAO ESP32S3 Sense │  │ MechDog ESP32        │
        │ 보조배터리 USB 급전   │  │ 커스텀 Arduino 펌웨어  │
        │ 카메라 → MJPEG 송출  │  │ HAL + 온보드 안전 로직 │
        └────────────────────┘  └──────────────────────┘
                  ✕ ─── 노드 간 배선 없음 ─── ✕
```

### 3-Tier 레이턴시 분리

| 계층 | 위치 | 담당 | 지연 |
| :--- | :--- | :--- | :--- |
| **Reflex** | MechDog ESP32 | 충돌 정지, 페일세이프, 저전압/전도 대응 | < 50 ms |
| **Cognition** | Host PC | 사람 인지 · PPE 판정 · 변화 감지, 행동 FSM, [P2] SLAM | 100~200 ms |
| **Reasoning** | 클라우드 | VLM 현장 판독, 이벤트 리포트 | 1~3 초 |

> **불변 규칙**: 안전 판단은 절대 온보드 밖으로 내보내지 않는다. Host PC가 꺼져도 로봇은 스스로 멈춘다.

---

## 핵심 기능

### Phase 1 — ROS2 미사용 (여기서 완결된 산출물이 나옵니다)

**기반**

| | 기능 | 내용 |
| :--- | :--- | :--- |
| FR-1 | 제어 링크 · 페일세이프 | 300ms 명령 타임아웃, 링크 두절 · 저전압 · 전도 시 자동 안전 정지 |
| FR-2 | 자율 순찰 · 장애물 회피 | Trot 보행, 초음파 25cm 온보드 반사 정지, 정지 후 주변 스캔 |
| FR-3 | 사람 인지 · 추적 | 객체 검출 → 추적 ID 부여 → 경계 자세 → 타겟 락온 추종 |
| FR-4 | 웹 미션 대시보드 | FPV 스트리밍, 텔레메트리 차트, 수동 오버라이드, E-Stop, 이벤트 피드 |

**임무 — 이 셋이 프로젝트의 본체입니다**

| | 기능 | 내용 |
| :--- | :--- | :--- |
| FR-8 | **사이클 간 변화 감지** | 구역별 기준 객체 목록을 저장하고, 다음 순찰에서 **없어진 물건 · 새로 생긴 물건**을 판정합니다. 픽셀 차분이 아니라 객체 목록 비교입니다 |
| FR-9 | **산업 안전 관리 (PPE)** | 안전모 · 안전조끼 미착용을 감지합니다. 카메라 높이가 15cm라 가까이서는 머리가 잘리므로, **자세를 단계적으로 올려** 시야를 확보한 뒤 재판정합니다 |
| FR-10 | **경비 인증** | 사원증 ArUco → 실패 시 **음성 암구호**(온칩 오프라인 인식) → 실패 시 L3 경보. 눈 LED 색으로 단계를 표시합니다 |

> 세 기능은 모두 **같은 순찰 사이클 안에서** 동작합니다. 사람이 검출되면 그 프레임에서 PPE를 판정하고, 미인증 인원이면 인증 절차로 넘어가며, 사람이 없는 구역에서는 물체 변화를 비교합니다.

### Phase 2 — 측위 확장 (조건부)

| | 기능 | 내용 |
| :--- | :--- | :--- |
| FR-6 | LiDAR SLAM | LD19 + ROS2 `slam_toolbox` 맵 생성, 웨이포인트 순찰 |
| FR-7 | 구역 순찰 · 랜덤 자율주행 | A~E 구역 간 자율 이동, 위험구역 긴급 진입 |

> Phase 2 착수 조건: MechDog 탑재 여력이 약 100g 이상일 것. 미달 시 스마트폰 VIO 또는 ArUco로 대체합니다.
>
> **FR-8은 Phase 2가 아닙니다.** 변화 감지에 필요한 건 "지금 어느 구역인가"뿐이고, 그건 구역마다 마커 한 장이면 됩니다. 정밀 좌표가 필요한 것은 구역 *사이를 이동하는* FR-7 쪽입니다.

---

## 하드웨어

| 노드 | 장비 | 전원 |
| :--- | :--- | :--- |
| Motion | Hiwonder MechDog (Advanced Kit) — ESP32, 8× 코어리스 서보, IMU, 초음파 | 2S 리튬 7.4V (순정) |
| Vision | Seeed XIAO ESP32S3 Sense — OV2640, 8MB PSRAM | **보조배터리 USB-C** (DR-10) |
| Host | Windows 11 + WSL2 Ubuntu 24.04, RTX 3080 | — |
| LiDAR *(P2)* | **FHL-LD19 (D500 키트)** + ESP32-DevKitC V4 중계 | 보조배터리 공용 |

---

## 마일스톤

| | 내용 | ROS2 | 상태 |
| :--- | :--- | :---: | :--- |
| **M0** | 하드웨어 검증 (Wi-Fi 가용성, 무게 실측, 3대 캘리브레이션) | — | ⬜ 진행 예정 |
| **M1** | 제어 링크 & 페일세이프 | ✕ | ⬜ |
| **M2** | 비전 파이프라인 & 대시보드 | ✕ | ⬜ |
| **M3** | 행동 FSM 통합 — **여기서 완결된 산출물** | ✕ | ⬜ |
| **M4** | LiDAR & 매핑 | ○ | ⬜ 조건부 |
| **M5** | 웨이포인트 순찰 & 위험구역 출동 | ○ | ⬜ 조건부 |

👉 **다음 작업: [M0 하드웨어 검증 체크리스트](docs/HARDWARE_VERIFICATION.md)**

---

## 프로젝트 구조

```
mechdog_physical_ai/
├── docs/
│   ├── PRD_Physical_AI_Guard_Robot.md   # 요구사항 (FR · 마일스톤 · 리스크 · OI)
│   ├── ARCHITECTURE.md                  # 구조 · FSM · 품질 기준 · 용어
│   ├── DECISIONS.md                     # 설계 결정 기록 (ADR 18건)
│   ├── PROTOCOL.md                      # 통신 메시지 정본 (명령 8종)
│   ├── WBS.md                           # 작업 분해 · 일정 · 추적 매트릭스
│   ├── ASSIGNMENTS.md                   # 담당자별 작업 목록 (WBS 에서 생성)
│   ├── ENGINEERING_GUIDE.md             # 로깅 · 테스트 · CI 구현 기준
│   ├── HARDWARE_ASSEMBLY.md             # 조립 · 배선 · 전원 (무납땜)
│   └── HARDWARE_VERIFICATION.md         # M0 검증 체크리스트
├── config/
│   ├── config.yaml                      # 전역 파라미터 (매직 넘버 0개 목표)
│   ├── devices/                         # 개체별 프로파일 (서보 오프셋 등)
│   └── .env.example                     # 시크릿 템플릿
├── firmware_mechdog_motion/src/         # MechDog ESP32 (Arduino)
├── firmware_xiao_vision/src/            # XIAO ESP32S3 (Arduino)
├── host/                                # Host PC (Python)
│   ├── vision/                          # 스트림 수신 · 객체 검출 추론
│   ├── behavior/                        # FSM · 명령 송신
│   ├── telemetry/                       # 텔레메트리 수신
│   ├── dashboard/                       # FastAPI + WebSocket + UI
│   └── common/                          # 통신 규약 · 로깅 · config
├── tests/                               # pytest (하드웨어 불요)
├── tools/                               # 목업 · 지연 측정 · 텔레오퍼레이션
├── third_party/                         # HW_MechDog 벤더링
├── models/                              # ONNX 가중치 (git 제외)
├── maps/                                # 지도 산출물 (git 제외)
└── .github/
    ├── workflows/ci.yml                 # CI/CD
    ├── ISSUE_TEMPLATE/
    └── PULL_REQUEST_TEMPLATE.md
```

## 문서 지도 — 언제 무엇을 보나

| 상황 | 문서 |
| :--- | :--- |
| **내 할 일만 보고 싶다** | **[담당자별 작업 목록](docs/ASSIGNMENTS.md)** |
| **파서·직렬화를 구현한다** | [PROTOCOL.md](docs/PROTOCOL.md) ← **PRD 가 아니다** |
| 무엇을 만드는가 (기능 요구사항) | [PRD](docs/PRD_Physical_AI_Guard_Robot.md) |
| 어떤 구조인가 · FSM · 품질 기준 | [ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| **왜 이렇게 정했나 · 무엇을 왜 안 했나** | [DECISIONS.md](docs/DECISIONS.md) |
| **모르는 용어가 나왔다** | [ARCHITECTURE.md](docs/ARCHITECTURE.md) 부록 |
| 로깅 · 테스트 · CI 를 짠다 | [ENGINEERING_GUIDE.md](docs/ENGINEERING_GUIDE.md) |
| 브랜치 · PR · 코드 규약 | [CONTRIBUTING.md](CONTRIBUTING.md) |
| 조립 · 배선 · 발주 | [HARDWARE_ASSEMBLY.md](docs/HARDWARE_ASSEMBLY.md) |
| M0 검증 | [HARDWARE_VERIFICATION.md](docs/HARDWARE_VERIFICATION.md) |
| 내 워크패키지 · 일정 | [WBS.md](docs/WBS.md) |

## 처음 보면 틀리기 쉬운 것

| | |
| :--- | :--- |
| **측위 결정이 늦어도 착수는 안 막힌다** | Phase 1 의 P1 7종은 측위를 쓰지 않는다. 변화 감지조차 마커 한 장이면 된다 |
| **로봇은 걷는 사람을 못 따라간다** | 10~30cm/s 대 120~150cm/s. **판정 대상은 정지한 작업자**다 (FR-9.2.0) |
| **자세를 올린 상태로는 이동할 수 없다** | 전방 지면이 안 보인다. 대상이 움직이면 루프에 빠지므로 재시도 상한이 있다 (FR-9.2.4) |
| **ArUco 마커와 ARCore 앵커는 다르다** | 마커는 인쇄물, 앵커는 가상 좌표. 구역 마커는 Phase 2 에서 불필요해지지만 **사원증은 대체 불가** |
| **눈 LED 는 장식이 아니다** | 관측 가능한 상태 출력이며 디버깅 수단이다 |
| **L3 는 관리자 확인으로만 해제된다** | PPE 위반·물체 변화는 인증할 주체가 없다 |
| **`FR-8.0` 은 Phase 1 전용 우회 수단** | 측위가 확보되면 폐기된다 |

## 팀 구성

| 역할 | 담당 | 적용 범위 |
| :--- | :--- | :--- |
| **A · 임베디드** | 하드웨어 검증 · 모션 펌웨어 · 온보드 안전 로직 | 로봇 3대 공통 |
| **B · 인지·AI** | 비전 노드 · 객체 검출 · 대시보드 화면 | 로봇 3대 영상, Host PC 추론 |
| **C · 시스템·통합** | 통신 규약 · FSM · 대시보드 서버 · CI/CD · 문서 | Host PC와 전체 Fleet |

> A/B/C는 사람 이름이 아니라 작업 성격이다. 실제 인원 배정은 [WBS 4절](docs/WBS.md)을 따른다.
> 기준기는 Phase별로 나뉜다. `phase1_reference`는 LiDAR 미장착 P1 표준 구성,
> `phase2_reference`는 LiDAR 장착 2대 중 측위 검수용 1대다. 상세는 [CONTRIBUTING.md](CONTRIBUTING.md).

## 엔지니어링 원칙

| 축 | 적용 |
| :--- | :--- |
| **파라미터화** | 매직 넘버 0개. 전 상수를 `config.yaml`로 분리, Dev/Prod 프로파일 |
| **예외 처리** | 패킷 손실·역전 방어, 스트림 지수 백오프 재연결, Graceful Degradation |
| **성능** | 추론 워커 분리, 최신 프레임 우선 드롭 정책, 누수 점검 |
| **로깅** | JSON Lines 구조화 로그 + 레벨링 + 로테이션 + 이벤트 블랙박스 |
| **CI/CD** | FSM 전이·패킷 파싱·안전 판정을 **하드웨어 없이** pytest로 전수 검증 |
