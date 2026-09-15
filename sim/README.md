# sim/ — 디지털 트윈 (Isaac Sim)

실제 공간을 시뮬레이션으로 재현해 **로봇 시점의 PPE 학습 데이터**를 만들고,
라이다로 잰 실제 공간에서 **자율주행 동선을 검증**한다.
설계 근거는 `docs/MODEL_PLAN.md` 5절이다.

## 환경 — ✅ 이미 설치됨

`C:/Users/a9800/isaac_clean/venv` 에 **Isaac Sim 6.0.1** (Python 3.12 venv)이
있다. 새로 설치하지 않는다 — 이 venv 를 그대로 쓴다:

```bash
C:/Users/a9800/isaac_clean/venv/Scripts/python.exe sim/<script>.py
```

⚠️ 6.x 는 Python 3.12 지원. 5.x 문서의 "3.11 필수"는 이 설치본에 해당 없다.

## 두 환경 — 같은 로봇 코드, 환경만 교체

| 환경 | 씬 | 용도 |
| :--- | :--- | :--- |
| **A. factory** | 실사풍 공장 + 사람 + 물건 | PPE 이미지 학습 데이터·행동 시뮬레이션 |
| **B. lidar_room** | `room_spec.yaml` 로 재현한 실제 공간 | 자율주행 동선 계획·검증 |

## 파이프라인

```
room_spec.yaml (평면도 스펙)
   │  ← 입력원 두 가지: ① 줄자 실측 (지금)  ② LD19 → lidar_slam.py → OccupancyGrid (배선 후)
   ▼
씬 생성 (벽 압출·바닥·조명)        ── LabKeeper lab_world.py 기법 이식
   ▼
MechDog 프록시 + 카메라 — 15cm · 7° 하향 · H32°/V24° · 640×480  ← 실측값
   ▼
Replicator — 사람 위치·자세·착용 4조합·조명 무작위화
   ▼
RGB + 2D tight bbox writer → YOLO 라벨 변환 → datasets/ppe/ (session=sim_vN)
   │
   └─▶ 카메라 프레임 → MJPEG 웹 스트림 (LabKeeper :8080 패턴 이식)
```

## 재사용 자산 — LabKeeper (`../LabKeeper/robot-sim/`)

새로 짜지 않고 이식한다: `lab_world.py`(씬 조립) · `generate_isaac_dataset.py`
(헤드리스→YOLO) · `isaac_hal.py`(로봇 HAL) · `waypoint_controller.py`+
`obstacle_avoidance.py`(동선 실행+회피) · `live_patrol_stream.py`(웹 스트림).
차이점 — 저쪽은 Raspbot(바퀴), 우리는 MechDog(사족). 시뮬에서 보행 물리는
흉내내지 않고 카메라 포즈만 맞춘다(미끄러지듯 이동해도 데이터 목적엔 충분).

## 파일 구성

| 파일 | 역할 |
| :--- | :--- |
| `mechdog_spec.py` | 실측 스펙 정본 — 치수·질량·카메라 포즈·속도·안전 타이밍 |
| `mechdog_proxy.py` | 실측 치수 USD 프록시 + 키네마틱 구동 (MOVE 호 조향 그대로) |
| `factory_world.py` | 환경 A — NVIDIA `full_warehouse.usd` 실사 창고 (실패 시 절차 씬 폴백) |
| `people_spawner.py` | PPE 4조합 근로자 배치 — 캐릭터 에셋 우선, 합성 인형 폴백 |
| `protocol_server.py` | UDP 명령(:5001)/텔레메트리(:5101) — 실기와 같은 `CommandDecoder`/`TelemetryEncoder` |
| `web_stream.py` | FPV 카메라 → MJPEG `:8080/stream` |
| `run_factory.py` | 통합 러너 — 씬+로봇+사람+프로토콜+스트림 |
| `gen_ppe_dataset.py` | 합성 PPE 데이터 생성 — 위치 무작위화 + 자동 YOLO 라벨 |
| `smoke_factory.py` | 헤드리스 스모크 테스트 |
| `capture_preview.py` | FPV/전경 미리보기 PNG 저장 (`--with-people`, `--mat-floor`) |
| `probe_warehouse.py` | 씬 바운딩박스 측정 — 배치 좌표 확인용 |
| `room_spec.yaml` | B 환경용 평면도 스펙 — 라이다 격자 or 줄자 실측 |

## 실행

```bash
# 공장 씬 + 시뮬 로봇 + 웹 스트림 + 프로토콜 서버 (전체)
C:/Users/a9800/isaac_clean/venv/Scripts/python.exe sim/run_factory.py

# 헤드리스 (렌더만, 창 없음)
... sim/run_factory.py --headless --workers 4

# 스모크 테스트 — 씬 구축·카메라·키네마틱 검증
... sim/smoke_factory.py
```

떠 있는 동안 **실기용 도구가 그대로 붙는다** — `--robot` 에 시뮬 PC 의 IP 만 주면 된다:

```bash
python tools/teleop.py --robot 127.0.0.1
python tools/mechdog_command.py --robot 127.0.0.1 MOVE step=60 angle=0
```

브라우저 `http://localhost:8080/stream` → 로봇 FPV 생중계.

## 합성 PPE 데이터 생성

```bash
C:/Users/a9800/isaac_clean/venv/Scripts/python.exe \
    sim/gen_ppe_dataset.py --count 50 --preview datasets/ppe/preview/train
```

프레임마다 작업자 위치·방향·착용 4조합과 로봇 포즈를 무작위화하고, **스폰
정답**(누가 안전모·조끼를 입었는지 우리가 안다)으로 라벨을 만든다 — 색 추정이
아니다. 캐릭터 에셋도 `hardhat`·`safetyvest` 메시를 꺼서 진짜 4조합이 된다.
출력은 `datasets/ppe/{images,labels}/train/<session>/` + `_manifest.csv` —
합성은 **train 전용**, val/test 는 실사만 쓴다 (`datasets/README.md` 참조).

⚠️ 라벨 박스는 prim 바운드가 아니라 **작업자 위치 + 표준 인체 대역**으로 만든다
— 참조 캐릭터의 `ComputeWorldBound` 는 스폰 위치가 빠진 로컬 바운드를 돌려준다.
머리 대역(1.48~1.78m)·몸통 대역(0.80~1.42m)은 실기 카메라(15cm·-7°)가 실제로
볼 수 있는 범위와 일치한다 — 머리는 ~17m 이상에서만 프레임에 들어오므로
근거리 프레임은 `no_helmet` 판정 자체가 불가하다(실기도 마찬가지 — 물리다).

## 전체 스택 듀얼 — 대시보드까지 시뮬에 붙이기

`config/devices/mechdog-sim.yaml` 프로파일 하나로 **실기용 전체 호스트 스택**
(명령 서비스 + 비전 파이프라인 + 대시보드)이 시뮬레이터에 붙는다.
위쪽 코드는 시뮬과 실기를 구분하지 못한다 — 이것이 듀얼 구조의 의미다.

```bash
# 터미널 1 — 시뮬레이터 (Isaac venv)
C:/Users/a9800/isaac_clean/venv/Scripts/python.exe sim/run_factory.py --headless

# 터미널 2 — 호스트 런타임 + 관제 (repo .venv)
.venv/Scripts/python.exe -m host.runtime --device mechdog-sim --dashboard-port 8001
```

브라우저에서 `http://localhost:8001/live` — FPV 영상 + E-STOP + 수동 패드가
실기와 같은 화면으로 동작한다. `/camera/stream` 은 비전 워커가 추론에 쓴
바로 그 프레임을 다시보낸다 — **화면에 보이는 것 = 판정에 들어간 것.**

⚠️ **실기 런타임과 같은 PC 에서 동시에 띄울 때**: `mechdog-sim.yaml` 의
`telemetry_port` 는 `5102` 다 — 실기용 런타임이 5101 을 점유 중이면 Windows
는 같은 UDP 포트를 둘째에게 주지 않기 때문이다. 시뮬은 텔레메트리를
**명령이 온 소켓의 포트로** 돌려내므로(protocol_server 의 peer 규칙)
호스트 쪽만 포트를 바꾸면 된다. 대시보드 포트도 마찬가지로 겹치면 안 된다.

검증된 것 (2026-09-15, 시뮬에서):
- `/live` 페이지·`/api/telemetry`·`/camera/stream` 정상
- 수동 모드 게이트 → drive 거절 → MANUAL 진입 → 전진·회전 명령이 시뮬 로봇을 움직임
  (yaw 90°→223° 회전, FPV 전진 확인)
- E-STOP 즉시 FAILSAFE + sim `safety_latched`, RESET_SAFE 로 복귀
- 시뮬이 죽으면 호스트가 LINK_LOST → FAILSAFE 로 떨어짐 (안전 경로 정상)

## 알려진 이슈

- 대시보드 텔레메트리는 `motion`(x·y) 확장 필드를 아래로 안 내려준다 —
  위치 시각화가 필요하면 대시보드 쪽 확장이 필요하다.
- 카메라 수직 시야가 좁다(위쪽 +5°) — 머리는 ~17m·몸통은 ~10m 이상에서만
  보인다. 이건 시뮬 결함이 아니라 실기 카메라의 물리다 — 가까운 사람에게서
  PPE 라벨이 안 나오는 것은 정상이다.
- 캐릭터 에셋은 T-pose 라 시각적 리얼리티가 제한적 — 부위 라벨용으론 충분.

## 구현 노트 — 발견·수정한 것

- **USD 카메라 near-clip 기본값은 1m** — 15cm 카메라는 바로 앞 바닥이 잘려
  화면 하반이 검게 나온다. `set_clipping_range(0.01, ...)` 필수.
- `Camera` 의 `orientation` 은 `camera_axes="world"` 규약(로컬 +X=시선, +Z=위)을
  기본으로 받는다 — USD 의 `-Z` 시선과 다르니 quat 기저를 그에 맞춰 만든다.
- USD prim 이름에 `+`·`.` 불가 — 경로 생성 시 sanitize.
- `Shader.CreateInput` 은 `Sdf.ValueTypeNames.*` 타입 객체를 받는다.
- 참조된 캐릭터 prim 은 xformOp 가 이미 있어 `AddTranslateOp` 실패 —
  `UsdGeom.XformCommonAPI` 로 덮어쓴다.
- **캐릭터 에셋의 자체 xform 은 애니메이션이 매 스텝 덮어쓴다** — 루트에 직접
  `SetTranslate` 해도 스텝 후 (0,0,0)으로 되돌아온다. 부모 `Xform`(`worker_i`)
  아래에 캐릭터(`worker_i/char`)를 두고 **부모를 움직인다** — 자식이 뭘 쓰든
  부모 변환은 합성으로 살아남는다.
- `ComputeWorldBound` 는 참조 캐릭터에서 **스폰 위치가 빠진 로컬 바운드**를
  돌려준다 — 라벨·충돌 판정에 월드 바운드를 쓰면 안 된다.
- `camera.get_depth()` 는 annotator 를 `add_distance_to_image_plane_to_frame()`
  로 **먼저 부착해야** 값이 나온다 — 안 붙이면 None.
- 회전율 "spec 초과" 의혹은 검증 결과 **테스트 타이밍 착각**이었다 —
  curl 25회 POST 가 2초가 아니라 ~5초 걸린 것. 키네마틱 단독 검증은
  25.0°/s·0.104m/s·워치독 모두 정확 (`sim/mechdog_proxy.py` 의 수식 그대로).
- cp949 콘솔은 `—` 같은 문자에서 죽는다 — 실행 스크립트는
  `sys.stdout.reconfigure(errors="replace")` 로 시작한다.

⚠️ **생성 데이터는 session 이름에 `sim_` 접두어를 붙인다** — 검증셋에서 실사와
섞이지 않게 하고, datacard 의 `sources` 에 등록한다.
