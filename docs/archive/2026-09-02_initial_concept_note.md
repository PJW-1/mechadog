# [PRD] Physical AI 지능형 자율 경비·순찰 4족 보행 로봇
## Product Requirements Document (PRD) & System Architecture Specification

---

## 1. 문서 제어 및 개요 (Document Control & Overview)

| 항목 | 내용 |
| :--- | :--- |
| **프로젝트명** | MechDog Physical AI Security & Patrol Quadruped Robot |
| **문서 버전** | v1.0.0 |
| **상태** | Draft / In-Review |
| **작성일자** | 2026-09-02 |
| **대상 플랫폼** | Hiwonder MechDog (ESP32) + Seeed Studio XIAO ESP32S3 Sense |
| **저장소 위치** | `C:\Users\pjw\Desktop\mechdog_physical_ai` |

### 1.1 프로젝트 비전 (Project Vision)
본 프로젝트는 기존의 완구형 4족 보행 로봇(토이 레벨)의 한계를 탈피하여, **온디바이스 Edge AI(Vision & Audio)**와 **임베디드 모션 제어(8-DOF Inverse Kinematics)**를 융합한 **산업/시설물 대상 "피지컬 AI(Physical AI) 지능형 자율 경비·순찰 로봇"**을 개발하는 것을 목표로 한다. 

### 1.2 핵심 문제 정의 및 솔루션 (Problem Statement & Solution)
- **Problem**: 기존 교육용/취미용 4족 로봇은 단순 사전 정의 모션 재생이나 리모컨 수동 조작에 그쳐 자율적인 환경 판단, 위험 대응, 보안 순찰 기능을 수행하지 못함.
- **Solution**: 
  1. **Dual-MCU 이중화 아키텍처** (Perception Node + Actuation Node 분리)
  2. **행동 상태 머신(Behavior State Machine)** 기반의 자율 순찰, 장애물 능동 회피, 사람 인식 및 경비 자세(Alert Stance) 락온, 위험 구역 긴급 출동 파이프라인 구축.
  3. **임베디드 CI/CD 및 자동화 빌드/테스트 파이프라인**을 적용하여 산업용 수준의 소프트웨어 안정성 및 유지보수성 확보.

---

## 2. 시스템 아키텍처 및 하드웨어 구성 (Hardware & Physical Specs)

### 2.1 듀얼 MCU 토폴로지 (Dual-MCU Topology)

```
┌────────────────────────────────────────────────────────────────────────┐
│                   PERCEPTION & AI NODE (XIAO ESP32S3 Sense)            │
│  - Xtensa Dual-Core 240MHz + 8MB PSRAM                                 │
│  - OV2640 2MP Camera (Real-time Vision / Human Detection)              │
│  - Digital PDM Microphone (Acoustic Anomaly / Voice Wake-up)           │
│  - MicroSD Card (Blackbox Event / Telemetry Logger)                   │
│  - Wi-Fi 802.11 b/g/n (Web Mission Dashboard / WebRTC Stream / API)   │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ High-Speed UART (921600 bps)
                                    │ Full-Duplex Packet Protocol (CRC16)
┌───────────────────────────────────┴────────────────────────────────────┐
│                    ACTUATION & MOTION NODE (MechDog ESP32)             │
│  - ESP32 Dual-Core 240MHz                                              │
│  - 8 x High-Speed Coreless Servos (8-DOF Linkage Kinematics Engine)    │
│  - 6-Axis IMU (MPU6050: Auto-leveling, Dynamic Balance)               │
│  - Ultrasonic Sensor (HC-SR04 / Front Distance Obstacle Radar)         │
│  - Power Management & Battery Monitoring (2S Li-ion 7.4V Buck)         │
└────────────────────────────────────────────────────────────────────────┘
```

### 2.2 기구 및 하드웨어 사양
- **자유도 (DOF)**: 8-DOF (각 다리 2자유도 링크 구조: Hip/Shoulder + Knee)
- **보행 지원**: Trot Gait, Walk Gait, Crawl Gait, Spot Turn, Dynamic Body Pose (Pitch, Roll, Yaw, Height)
- **센서 구성**:
  - 카메라: OV2640 (최대 1600x1200, 30fps @ QVGA)
  - 거리 센서: 초음파 센서 (측정 범위 2cm ~ 300cm, 정밀도 ±3mm)
  - IMU: 6축 자이로/가속도 센서 (자세 추정 및 수평 유지 피드백)
  - 음향 센서: 고감도 디지털 PDM 마이크

---

## 3. 기능 요구사항 (Functional Requirements - FR)

### FR-1: 자율 순찰 및 반자율 장애물 회피 (Autonomous Patrol & Navigation)
- **FR-1.1**: 로봇은 지정된 순찰 모드(`STATE_PATROL`) 진입 시 전진 보행(Trot Gait)을 자율적으로 수행해야 한다.
- **FR-1.2**: 전방 25cm 이내에 장애물이 감지될 경우 즉시 전진을 멈추고 후진 또는 좌/우 회전 회피 기동(`STATE_AVOIDANCE`)을 수행해야 한다.
- **FR-1.3**: 순찰 중 일정 주기(예: 10초)마다 정지하여 상체 피치/요 각도를 제어하는 **주변 스캔 모션(`STATE_SCAN`)**을 3초간 수행하고 순찰을 재개해야 한다.

### FR-2: 사람 인지 및 경비 대응 엔진 (Person Detection & Guard Action)
- **FR-2.1**: 전방 카메라 영상에서 사람(Person/Face)이 탐지되면 즉시 경비 모드(`STATE_ALERT`)로 상태를 전환해야 한다.
- **FR-2.2 (Alert Stance)**: 몸체를 전방 상향(Pitch Up 15도)으로 세우고 시각적/물리적 경계 태세를 취해야 한다.
- **FR-2.3 (Target Lock-on & Tracking)**: 화면 내 사람의 중심 좌표(Centroid)를 추적하여 로봇 몸체를 좌/우로 회전시켜 타겟을 시야 중앙에 유지해야 한다.
- **FR-2.4 (Warning Broadcast)**: 침입자 인지 시 경고음(비프음 또는 오디오 방송) 및 경광 시각 신호를 발령해야 한다.
- **FR-2.5 (Event Logging)**: 탐지 시점의 스냅샷 이미지 및 타임스탬프를 로그에 기록하고 관제 대시보드로 실시간 푸시해야 한다.

### FR-3: 위험 구역 긴급 진입 및 정밀 탐색 (Hazard Zone Intervention)
- **FR-3.1**: 관제 센터 또는 비상 입력으로부터 특정 구역의 "위험 구역(Hazard Zone) 알람"을 수신하면 모든 일반 작업을 중단하고 즉시 긴급 출동 모드(`STATE_HAZARD_DISPATCH`)로 전환해야 한다.
- **FR-3.2 (Fast March)**: 고속 트롯 보행(Fast Trot)으로 해당 목표 위치로 이동해야 한다.
- **FR-3.3 (Hazard Inspection)**: 위험 현장 진입 후 360도 전방위 회전 스캔 및 정밀 카메라 뷰를 활성화하여 현장 위험원(화재 표식, 이상 물체 등)을 탐색해야 한다.
- **FR-3.4**: 현장 조사가 완료되면 관제 센터로 상태 보고(`HAZARD_INSPECTED_OK`)를 전송하고 복귀 또는 대기 상태로 전환해야 한다.

### FR-4: 실시간 FPV 및 웹 미션 관제 대시보드 (Web Mission Dashboard)
- **FR-4.1**: Wi-Fi AP 또는 로컬 네트워크에 연결하여 저지연(Low-Latency) MJPEG 비디오 스트리밍을 제공해야 한다.
- **FR-4.2**: 관제 웹 UI에서 가상 조이스틱을 통한 수동 텔레오퍼레이션(Teleoperation) 오버라이드 기능을 지원해야 한다.
- **FR-4.3**: 구역 관리자(Zone Manager)를 통해 [일반 순찰 구역 / 위험 구역]을 정의하고 비상 출동 명령을 1-Click으로 트리거할 수 있어야 한다.
- **FR-4.4**: 배터리 전압, IMU 자세, 현재 상태 머신(FSM), 감지된 타겟 정보 등의 텔레메트리를 실시간 차트/게이지로 표시해야 한다.

### FR-5: 이중 MCU 통신 프로토콜 (Inter-MCU High-Speed Protocol)
- **FR-5.1**: UART 직렬 통신 기반으로 921,600 bps의 고속 전이중 통신을 지원해야 한다.
- **FR-5.2**: 패킷 구조는 `[HEADER 2B][LEN 1B][CMD 1B][PAYLOAD NB][CRC16 2B]` 규격을 준수하여 노이즈로 인한 명령 왜곡을 100% 방지해야 한다.
- **FR-5.3**: 500ms 주기의 하트비트(Heartbeat) 패킷을 교환하여 통신 두절 시 자동 페일세이프(Emergency Stop)를 발동해야 한다.

---

## 4. 비기능 요구사항 (Non-Functional Requirements - NFR)

### NFR-1: 실시간성 및 응답 지연 (Performance & Latency)
- **NFR-1.1**: Edge AI 인지 루프 레이턴시: 사람 감지 및 바운딩박스 연산 주기 ≤ 66ms (≥ 15 FPS).
- **NFR-1.2**: 모션 제어 주기: 8개 서보 모터의 역기구학(IK) 및 PWM 갱신 주기 ≤ 20ms (50Hz 주기 완전 보장).
- **NFR-1.3**: FPV 비디오 스트리밍 지연 시간 ≤ 150ms (로컬 Wi-Fi 환경 기준).

### NFR-2: 신뢰성 및 안전성 (Reliability & Safety)
- **NFR-2.1 (Fail-Safe)**: 통신 단절, 비정상 패킷, 또는 배터리 저전압(6.4V 이하) 감지 시 로봇은 즉시 서보를 안전한 착지 자세(Crouch/Sit)로 변경하고 모터를 보호해야 한다.
- **NFR-2.2 (Watchdog)**: FreeRTOS 하드웨어 워치독 타이머(WDT)를 활성화하여 펌웨어 행(Hang) 발생 시 1초 이내 자동 리셋되어야 한다.
- **NFR-2.3 (Tipping Recovery)**: IMU 롤/피치 각도가 45도 이상 기울어져 전도(Fall-over)되었을 경우 모터를 셧다운하여 서보 기어 파손을 방지해야 한다.

### NFR-3: 코드 품질, 모듈화 및 이식성 (Code Quality & Maintainability)
- **NFR-3.1**: 하드웨어 종속 레이어(HAL), 역기구학 엔진(Kinematics), 상태 머신(FSM), 인지 계층(Perception) 간의 계층형 소프트웨어 아키텍처(Layered Architecture)를 엄격히 준수한다.
- **NFR-3.2**: 전역 변수 오염을 배제하고 객체 지향 C++17 및 Clean Code 가이드라인을 준수한다.

---

## 5. CI/CD 및 자동화 엔지니어링 파이프라인 (DevOps & CI/CD Pipeline)

본 프로젝트는 GitHub Actions를 기반으로 한 **임베디드 펌웨어 전용 CI/CD 파이프라인**을 구축하여 코드 푸시마다 빌드 무결성 검증, 정적 분석, 단위 테스트를 자동 수행한다.

```
[ Git Push / PR ]
       │
       ▼
┌────────────────────────────────────────────────────────┐
│ GitHub Actions Automated CI/CD Workflow                │
│                                                        │
│  Step 1: Code Formatting & Linting                     │
│          - Clang-Format (코드 스타일 표준 검사)          │
│          - Cppcheck (메모리 누수 / 정적 분석)          │
│                                                        │
│  Step 2: Kinematics Unit Testing                       │
│          - Unity Test Framework                        │
│          - 8-DOF Inverse Kinematics 수학 검증          │
│          - Protocol Packet Serialize / CRC16 검증      │
│                                                        │
│  Step 3: Multi-Target Firmware Compilation             │
│          - PlatformIO Core CLI                         │
│          - Build [env:xiao_esp32s3_sense]              │
│          - Build [env:mechdog_esp32_motion]            │
│                                                        │
│  Step 4: Artifact Packaging & Release                  │
│          - Binary Build Artifacts (.bin, .elf) 추출    │
│          - Semantic Versioning Release 태그 생성       │
└────────────────────────────────────────────────────────┘
```

---

## 6. 피지컬 AI 행동 상태 전이표 (State Transition Table)

| 현재 상태 (Current State) | 트리거 이벤트 (Trigger Event) | 다음 상태 (Next State) | 수행 액션 (Action) |
| :--- | :--- | :--- | :--- |
| `IDLE_STANDBY` | 순찰 명령 수신 (`CMD_START_PATROL`) | `STATE_PATROL` | Trot 보행 시작, 센서 활성화 |
| `STATE_PATROL` | 전방 초음파 < 25cm | `STATE_AVOID` | 정지 후 좌/우 회피 보행 |
| `STATE_PATROL` | 카메라 사람 인지 (`PERSON_DETECTED`) | `STATE_ALERT` | Pitch Up 경계 자세, 경고음 발령 |
| `STATE_PATROL` | 순찰 타이머 10초 만료 | `STATE_SCAN` | 정지 후 좌/우/상/하 360도 스캔 |
| `STATE_ALERT` | 타겟 이동 감지 | `STATE_TRACK` | 타겟 방향 회전 및 추종 보행 |
| `STATE_ALERT` | 타겟 시야 이탈 (5초 지속) | `STATE_PATROL` | 일반 순찰로 복귀 |
| *ANY STATE* | 위험 구역 알람 수신 (`HAZARD_ALARM`) | `STATE_HAZARD_DISPATCH` | 최우선 긴급 출동, Fast Trot |
| `STATE_HAZARD_DISPATCH`| 목표 구역 도착 | `STATE_HAZARD_SCAN` | 정밀 위험원 스캔 및 데이터 송신 |
| *ANY STATE* | 하트비트 타임아웃 / 저전압 | `STATE_FAILSAFE` | 긴급 정지, 서보 토크 해제 / 엎드리기 |

---

## 7. 단계별 구현 및 검증 마일스톤 (Milestones & Verification Plan)

| 마일스톤 | 핵심 산출물 | 검증 기준 (Acceptance Criteria) |
| :--- | :--- | :--- |
| **M1. 프로젝트 환경 & CI/CD** | PlatformIO 환경, GitHub Actions 워크플로우 | CI 상에서 양쪽 펌웨어 클린 컴파일 통과 |
| **M2. 하드웨어 통신 파이프라인** | UART CRC16 통신 라이브러리, 하트비트 | 921600 bps 무손실 패킷 송수신 10만 회 검증 |
| **M3. 모션 & IK 엔진** | 8-DOF IK 클래스, 5대 보행 모드 | Trot, Walk, Pitch/Roll 제어 정상 동작 |
| **M4. Edge AI 비전 & FPV** | XIAO 카메라 스트리머, Person Detector | 15+ FPS 스트리밍 및 실시간 사람 박스 검출 |
| **M5. FSM 행동 엔진 & 대시보드**| 순찰-경비-위험진입 통합 FSM, 웹 대시보드 | 시나리오별 자동 상태 전이 및 웹 원격 제어 성공 |
