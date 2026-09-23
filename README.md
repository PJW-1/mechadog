# MechDog Physical AI — 경비 · 산업안전 점검 4족 로봇

## 관제 화면

**[관제 대시보드](docs/DASHBOARD.md)** — 3D 현장 관제, 작업 페이지, 수동 제어 · 비상정지. 화면은
`host/dashboard/static/` 에 있고 런타임의 관제 서버가 **설치 없이** 내보냅니다.

```sh
python -m host.runtime --device mechdog-01 --robot-ip <로봇 IP> --no-vision --dashboard-port 8000
```

http://127.0.0.1:8000/#dashboard 를 엽니다(실제 장비 명령은 `제어 · 장치` 화면). 로봇 없이 보려면 가상 로봇
`python tools/mock_mechdog.py --device mechdog-01` 을 먼저 띄우고 `--robot-ip 127.0.0.1` 로 실행합니다.
⚠️ 서버가 내보낸 화면의 수동 조작은 **실제 로봇을 움직입니다.**

Hiwonder MechDog(ESP32)과 Seeed XIAO ESP32S3 Sense, Host PC를 결합한 **자율 순찰 4족 보행 로봇** 프로젝트입니다.

로봇은 순찰마다 **운용 모드** 하나를 골라 돕습니다 — **경비 모드**(침입자 인지·인증·경보)와 **공장 모드**(보호구 점검·물체 변화·위험 상황) 두 가지입니다. 한 사람에게 서로 다른 판단을 동시에 적용하지 않도록 **한 순찰에서 하나만** 사용합니다([ADR-33](docs/DECISIONS.md#adr-33)). 세 번째였던 현장지원 모드(가상 MES 운영정보 질의응답)는 2026-09-23 폐기했습니다([ADR-38](docs/DECISIONS.md#adr-38)).

MechDog 제조사(Hiwonder)가 제공하는 모션 라이브러리(`HW_MechDog`)를 **HAL로 취급**하고, 그 위에 **인지 → 판단 → 항법** 자율 스택을 새로 얹는 것이 목표입니다. 이 라이브러리는 라이선스 표기가 없어 저장소에 넣지 않습니다([ADR-20](docs/DECISIONS.md)).

📄 **[PRD v1.0 — 요구사항 및 설계 결정](docs/PRD_Physical_AI_Guard_Robot.md)**  
📋 **[WBS — 작업 ID·선행·DoD 정본](docs/WBS.md)**<br>
🙋 **[담당자별 작업 목록 — 지금 할 일](docs/ASSIGNMENTS.md)** ← 매일 보는 문서<br>
🧭 **[설계 결정 기록 — 무엇을 왜 안 했나](docs/DECISIONS.md)** ← ADR 38건<br>
🔌 **[하드웨어 — 착수 확인 · LiDAR 배선 · 발주](docs/HARDWARE.md)**  
🛠️ **[엔지니어링 가이드 — 로깅·테스트·CI](docs/ENGINEERING_GUIDE.md)**  
📡 **[통신 프로토콜 정본 — 명령 10종·검증 규칙](docs/PROTOCOL.md)**
🏗 **[시스템 아키텍처 — 구조·상태·품질 기준](docs/ARCHITECTURE.md)** ← 용어 부록 포함  
🤝 **[협업 규칙](CONTRIBUTING.md)**

> **처음 클론했다면** — `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1` (약 40초)
>
> **로봇 펌웨어(구동·센서)를 빌드하려면** 벤더 파일을 따로 받아야 한다 — [펌웨어 README](firmware_mechdog_motion/README.md)의 *구동·센서 통합 빌드 준비* 절, 점검은 `python tools/firmware_env.py`

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
| **Reflex** | MechDog ESP32 | 충돌 정지, 링크 두절·저전압 페일세이프 | < 50 ms |
| **Cognition** | Host PC | 사람 인지 · PPE 판정 · 변화 감지, 행동 FSM, [P2] SLAM | 100~200 ms |
| **Reasoning** | Host PC (로컬) | VLM 단일 장면 판독, 이벤트 리포트. 클라우드 연동은 P2 범위 | 비실시간 |

2026-09-09 실기에서 XIAO VGA 25fps 수신 중에도 메인 ESP32 명령 RTT는 평균
3.2ms·최대 15.4ms·손실 0%였다. **통신은 병목이 아니다.**

2026-09-10 실기로 인지 구간까지 쟀다 — **YOLOX-S** 검출이 기준 PC(RTX 3080·DirectML)에서
**p95 8.6ms**, 2~3m 사람 검출 **0.89~0.92**. 추론률을 수신률과 같은 **25fps** 로 올려
프레임 도착→검출 완료가 **49.9 → 24.4ms** 로 줄었다. 사람 판정은 **300ms 안에 3회**이며
연속 프레임 대신 고정 시간 창을 쓴다. 다만 확인 지연과 히트 기회는 추론률의 영향을
받으므로 **25fps까지 포함해 실측값으로 고정**했다 ([ADR-25](docs/DECISIONS.md)).

> **불변 규칙**: 안전 판단은 절대 온보드 밖으로 내보내지 않는다. Host PC가 꺼져도 로봇은 스스로 멈춘다.

---

## 핵심 기능

### Phase 1 — ROS2 미사용 (여기서 완결된 산출물이 나옵니다)

**기반**

| | 기능 | 내용 |
| :--- | :--- | :--- |
| FR-1 | 제어 링크 · 페일세이프 | 300ms 명령 타임아웃, 링크 두절 · 저전압 시 자동 안전 정지. 전도 자동 감지는 폐기(ADR-36); 상시 인적 감시와 E-Stop 적용 |
| FR-2 | 자율 순찰 · 장애물 회피 | Trot 보행, 초음파 25cm 온보드 반사 정지, 정지 후 주변 스캔 |
| FR-3 | 사람 인지 · 추적 | 객체 검출 → 추적 ID 부여 → 경계 자세 → 타겟 락온 추종 |
| FR-4 | 웹 미션 대시보드 | FPV 스트리밍, 텔레메트리 차트, 수동 오버라이드, E-Stop, 이벤트 피드 |

**임무 — 핵심 3개**

| | 기능 | 내용 |
| :--- | :--- | :--- |
| FR-8 | **사이클 간 변화 감지** · 공장 모드 | 구역별 기준 객체 목록을 저장하고, 다음 순찰에서 **없어진 물건 · 새로 생긴 물건**을 판정합니다. 픽셀 차분이 아니라 객체 목록 비교입니다 |
| FR-9 | **산업 안전 관리 (PPE)** · 공장 모드 | 안전모 · 안전조끼 미착용을 감지합니다. 카메라 높이가 15cm라 가까이서는 머리가 잘리므로, **자세를 단계적으로 올려** 시야를 확보한 뒤 재판정합니다 |
| FR-10 | **경비 인증** · 경비 모드 | 사원증 ArUco → 실패 시 **음성 암구호**(PC 로컬 한국어 인식) → 실패 시 L3 경보. 눈 LED 색으로 단계를 표시합니다 |

> 기능은 **운용 모드로 나뉩니다**(FR-11 · [ADR-33](docs/DECISIONS.md#adr-33)). **경비**는 침입 대응과 인증, **공장**은 보호구 판정·물체 변화·위험 상황 판독을 담당합니다. 모드는 순찰 전에 정하고 로봇이 멈춰 있을 때만 바꾸며, 바꿔도 경보·페일세이프는 풀리지 않습니다. 현재 두 모드 모두 선택 가능하지만 `factory` PPE 종단 실기는 아직 통과하지 않았다(`3.7.3`).
> 부가기능이던 FR-12 현장 정보 안내(현장지원 모드 · 문서 RAG · 가상 MES 조회)는 2026-09-23 폐기했습니다([ADR-38](docs/DECISIONS.md#adr-38) · ADR-34 폐기).
>
> **음성은 규칙만 씁니다.** PC 가 한국어 음성을 faster-whisper 로 알아듣고, 웨이크워드·화이트리스트 로봇 명령·시나리오·비상 호출·암구호 규칙에 걸린 말에만 정해진 문장으로 답합니다. 규칙 밖의 말에는 「잘 못 들었습니다. 다시 말씀해 주세요.」 한 줄로 답하며 음성 LLM 은 쓰지 않습니다. 목표 경로는 **XIAO 마이크(포트 82)로 듣고 로봇의 MP3 모듈(I²C `0x7B`)로 말하는 것**이고(`4.7.19`~`4.7.21`), 지금 코드는 WonderEcho 모듈을 USB COM 으로 잇는 임시 링크로 Piper 음성을 실시간 합성해 냅니다([ADR-38](docs/DECISIONS.md#adr-38)).

### Phase 2 — 측위 확장 (조건부)

| | 기능 | 내용 |
| :--- | :--- | :--- |
| FR-6 | LiDAR SLAM | 12m급 2D LiDAR + ROS2 `slam_toolbox` 맵 생성, 웨이포인트 순찰 |
| FR-7 | 구역 순찰 · 랜덤 자율주행 | A~E 구역 간 자율 이동, 위험구역 긴급 진입 |

> ⚠️ **본선은 LiDAR 자율주행입니다.** 측위 트랙은 **Track A(2D LiDAR SLAM)로 확정**됐고(OI-9 닫힘 · [ADR-18](docs/DECISIONS.md)) 제품도 LD19 로 정해 중계 노드 펌웨어까지 들어와 있습니다. FR-6·FR-7 이 목표하는 것은 **지도를 만들고 그 위를 스스로 다니는 것**입니다.
>
> Phase 2 착수 조건: 보조배터리·마스트·센서를 포함한 실제 최종 구성으로 H3 탑재 검수를 통과할 것. 과거 100g 기준 대신 [하드웨어 검수](docs/HARDWARE.md)를 따릅니다.
>
> **ArUco 마커는 대안이 아니라 최후 예비(Track C)입니다.** H3 가 끝내 통과하지 못해 **LiDAR 를 실을 수 없다고 판정될 때에만** 구역 정의를 마커로 축소합니다. 마커로 가면 *"구역을 안다"* 는 남지만 **자율주행은 남지 않습니다** — 둘은 같은 것의 두 방식이 아닙니다.
>
> **FR-8(변화 감지)만 예외입니다.** 변화 감지에 필요한 건 "지금 어느 구역인가"뿐이라 Phase 1 에서는 구역 마커 한 장으로 갑니다(`FR-8.0`). 그건 **측위를 대신하는 것이 아니라 측위가 없는 동안의 우회**이고, 측위가 서면 폐기됩니다. 정밀 좌표가 필요한 것은 구역 *사이를 이동하는* FR-7 쪽입니다.

---

## 하드웨어

| 노드 | 장비 | 전원 |
| :--- | :--- | :--- |
| Motion | Hiwonder MechDog (Advanced Kit) — ESP32, 8× 코어리스 서보, IMU, 초음파 | 2S 리튬 7.4V (순정) |
| Vision | Seeed XIAO ESP32S3 Sense — OV3660(현행) / OV2640(구형), 8MB PSRAM | **보조배터리 USB-C** (DR-10) |
| Host | Windows 11 + WSL2 Ubuntu 24.04 — **RTX 3080 10GB · i7-10700K · RAM 32GB** (기준 PC) | — |
| LiDAR *(P2)* | **LD19 (D500 키트)** · 12m급 2D + 필요 시 ESP32-DevKitC 중계 | 보조배터리 공용 |

---

## 마일스톤

| | 내용 | ROS2 | 상태 |
| :--- | :--- | :---: | :--- |
| **M0** | 하드웨어 착수 확인 (H1 백업·H2 Wi-Fi·H3 탑재 보행, 3대 캘리브레이션) | — | 🔶 **H1·H2 통과** · H3 는 LiDAR 도착 후 |
| **M1** | 제어 링크 & 페일세이프 | ✓ | ✅ **G1 실기 통과 (2026-09-17)** — 조종·300ms 정지·텔레메트리 10Hz·저전압 페일세이프. 전도 자동 감지는 ADR-36으로 폐기 |
| **M2** | 비전 파이프라인 & 대시보드 | ✕ | ✅ **통과 (2026-09-15)** — VGA 17fps · 검출 박스 FPV 22.62fps · 자동 재연결 · **E2E 단일 프레임 81~121ms**(예산 250ms) |
| **M3** | 행동 FSM 통합 — **여기서 완결된 산출물** | ✕ | ⬜ |
| **M4** | LiDAR & 매핑 | ○ | ⬜ 조건부 |
| **M5** | 웨이포인트 순찰 & 위험구역 출동 | ○ | ⬜ 조건부 |

👉 **다음에 할 일은 [담당자별 작업 목록](docs/ASSIGNMENTS.md)의 🟢 항목이다.**
> 선행이 끝나 지금 잡을 수 있는 것만 모아 두었고 WBS 에서 생성되므로 낡지 않는다.

---

## 프로젝트 구조

```
mechdog_physical_ai/
├── docs/
│   ├── PRD_Physical_AI_Guard_Robot.md   # 요구사항 (FR · 마일스톤 · 리스크 · OI)
│   ├── ARCHITECTURE.md                  # 구조 · FSM · 품질 기준 · 용어
│   ├── DECISIONS.md                     # 설계 결정 기록 (ADR 35건)
│   ├── PROTOCOL.md                      # 통신 메시지 정본 (명령 10종)
│   ├── WBS.md                           # 작업 ID · 선행 · DoD 정본
│   ├── ASSIGNMENTS.md                   # 진행 현황 · 담당 목록 (WBS에서 생성)
│   ├── ENGINEERING_GUIDE.md             # 로깅 · 테스트 · CI 구현 기준
│   └── HARDWARE.md                      # 착수 확인 · LiDAR 배선 · 발주
├── config/
│   ├── config.yaml                      # 전역 파라미터 (매직 넘버 0개 목표)
│   ├── devices/                         # 개체별 프로파일 (서보 오프셋 등)
│   └── .env.example                     # 시크릿 템플릿
├── firmware_mechdog_motion/src/         # MechDog ESP32 (Arduino)
├── firmware_xiao_vision/                # XIAO ESP32S3 카메라 + MJPEG (Arduino)
├── host/                                # Host PC (Python)
│   ├── vision/                          # 스트림 수신 · 검출기 · 추론 워커 · 사람 게이트
│   ├── behavior/                        # FSM · 명령 송신
│   ├── telemetry/                       # 텔레메트리 수신
│   ├── dashboard/                       # FastAPI + WS + UI (4.5 · 4.6.1/3/4 완료)
│   └── common/                          # 통신 규약 · 로깅 · config
├── tests/                               # pytest (하드웨어 불요)
├── tools/                               # 목업 · 지연 측정 · 텔레오퍼레이션 · 가중치 받기
├── third_party/                         # 비어 있다 — 벤더 라이브러리는 라이선스 문제로 넣지 않는다 (ADR-20)
├── models/                              # ONNX 가중치 (git 제외 · `tools/fetch_models.py` 로 받는다)
├── maps/                                # 지도 산출물 (git 제외)
└── .github/
    ├── workflows/                       # CI/CD (ci.yml) · 웹 CI (web.yml)
    ├── ISSUE_TEMPLATE/
    └── PULL_REQUEST_TEMPLATE.md
```

## 호스트 런타임 실행

### 먼저 — 가중치를 받는다 (처음 한 번)

⚠️ **ZIP·clone 에는 ONNX 가중치가 들어 있지 않다.** 용량(`coco.onnx` 34MiB) 때문에
저장소에서 제외하며, 없으면 `Detector.open()` 이 `ModelMissingError` 로 즉시 멈춘다.

```powershell
pip install -r requirements.txt
python tools/fetch_models.py
```

두 번째 명령이 `models/coco.onnx`(사람 검출)와 `models/ppe.onnx`(현행 ppe-v3 보호구 5클래스)를
받고 크기·SHA-256을 검증한다. **검출은 2단이라 둘 다 있어야 한다** — `coco`가 찾은
사람 영역 안에서만 `ppe`가 돈다. 받는 곳과 라이선스는 [`models/README.md`](models/README.md)와
[`models/NOTICE`](models/NOTICE)에 있다.

### 런타임

비전 워커는 기본으로 함께 시작한다. DHCP로 받은 주소가 개체 프로파일에 아직 없으면
실행할 때 덮어쓴다.

```powershell
python -m host.runtime --device mechdog-01 --robot-ip <로봇-IP> --xiao-ip <XIAO-IP> --patrol
```

카메라와 모델을 제외하고 모션 링크만 진단할 때에만 `--no-vision`을 붙인다.

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
| 조립 · 배선 · 발주 · 착수 확인 | [HARDWARE.md](docs/HARDWARE.md) |
| **오늘 시작할 작업과 대기 작업** | **[ASSIGNMENTS.md](docs/ASSIGNMENTS.md)** |
| 작업의 상세 DoD·선행 관계 | [WBS.md](docs/WBS.md) |

## 처음 보면 틀리기 쉬운 것

| | |
| :--- | :--- |
| **측위 결정이 늦어도 Phase 1 착수는 안 막힌다** | Phase 1 필수 기능은 정밀 측위를 쓰지 않는다. 변화 감지조차 마커 한 장이면 된다. ⚠️ **다만 Phase 2 의 본선은 LiDAR 자율주행이며 마커가 그것을 대신하지 않는다** — 마커는 LiDAR 탑재 불가 판정 시의 최후 예비다 |
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

> A/B/C는 사람 이름이 아니라 작업 성격이다. 실제 인원 배정은 [ASSIGNMENTS](docs/ASSIGNMENTS.md)를 따른다.
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

## PC 관제 서버 개발

Host 런타임의 `--dashboard-port 8000` 옵션으로 로컬 텔레메트리 HTTP/WS 서버를
함께 실행한다. 다중 화면에 10Hz 상태를 전달하며 미수신·오래된 값을 구분한다.
실행·로봇 없는 시험·후속 화면/API 범위는 [관제 서버 안내](docs/DASHBOARD.md)를 참고한다.
