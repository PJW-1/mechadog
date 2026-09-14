# PC 관제 텔레메트리 서버 — WBS 4.5.1

기존 Host PC 런타임이 수락한 로봇 텔레메트리와 현재 FSM·에스컬레이션을
FastAPI/WebSocket으로 전달한다. 모델은 PC에서 실행한다. 별도 보드나 로봇
펌웨어 설치가 필요하지 않으며, 이 서버는 로봇 UDP 소켓을 추가로 열지 않는다.

## 실행

기존 런타임 명령에 `--dashboard-port 8000`을 붙인다. 옵션이 없으면 서버는 시작하지 않는다.

```powershell
python -m pip install -r requirements-dev.txt
python -m host.runtime --device mechdog-01 --dashboard-port 8000
```

이 명령은 **기존 로봇 운용 런타임도 실행한다.** 서버만 시험하려면 아래 pytest를 쓴다.
현재 작업에서는 런타임을 실물에 연결하지 않았다.

- `http://127.0.0.1:8000/health`: 서버 상태, 연결 수, 느린 클라이언트의 상태 갱신 합침 횟수.
- `http://127.0.0.1:8000/api/telemetry`: 최신 상태 한 건.
- `ws://127.0.0.1:8000/ws/telemetry`: 10Hz 상태 스트림.
- `http://127.0.0.1:8000/docs`: HTTP API 확인. 관제 화면은 후속 4.6 작업이다.

## 관제 화면의 three.js

`/` 로 서빙하는 `web/design-prototype` 은 3D 현장을 그리려고 three.js 를
`/vendor/three.module.js`, `/vendor/addons/...` 로 불러온다. **그 `vendor/` 폴더는
소스 트리에 없다** — 프로토타입 개발 서버(`scripts/server.mjs`)가 요청을
`node_modules/three` 로 돌려주고, `npm run build` 는 `build/vendor` 로 복사해 넣는다.

그래서 대시보드 서버도 같은 규칙으로 `/vendor/*` 를 붙인다. `resolve_web_root()`
가 **빌드본을 먼저** 고르고(`build/vendor` 가 있으면 `build/` 를 통째로 서빙),
없으면 소스 폴더의 `vendor/` 나 `node_modules/three` 를 쓴다.

```powershell
cd web/design-prototype
npm install     # 한 번만. three.js 를 받는다
```

⚠️ **`npm install` 을 건너뛰면 3D 만 비는 것이 아니라 화면이 통째로 죽는다.**
`app.js` 가 `scene.js` 를 정적으로 `import` 하고 그것이 `three` 를 끌어오므로,
vendor 가 없으면 모듈 실행이 **첫 줄에서 멈춘다** — 패널도 조이스틱도 비상정지도
나오지 않고 머리말만 남은 껍데기가 뜬다. 원인을 알려 주는 표시가 화면에 전혀 없다.

그래서 자산이 없으면 **프로토타입을 아예 띄우지 않는다.** `/` 는 `/live` 로 307
넘김이 되고, 서버 로그에 `web_prototype_unavailable` 과 고치는 명령이 남는다.
죽은 화면을 보여 주는 것보다 동작하는 최소 화면이 낫다.

## 관제 화면이 둘인 이유

혼동이 있었으므로 적어 둔다 — **두 화면은 서로 다른 파일이고 목적이 다르다.**

| 경로 | 파일 | 쓰임 |
| :--- | :--- | :--- |
| `/` | `web/design-prototype/` (여러 모듈 + three.js) | 전체 관제 프로토타입. 3D 현장·패널·사건·구역까지. 빌드가 필요하다 |
| `/live` | `host/dashboard/static/live.html` (한 파일 89줄) | 실기 최소 화면. 카메라 + 이동 + 비상정지만. **의존성이 없어 항상 뜬다** |

둘 다 같은 `/api/command/{manual,drive,estop}` 을 쓴다(`/live` 는 `reset` 도 쓴다).
**조작 경로가 갈라져 있지 않다** — 화면만 둘이다.

⚠️ **그래도 조이스틱과 비상정지 구현은 두 벌이다**(프로토타입은 `robot-link.js`,
`/live` 는 자체 인라인 스크립트). 한쪽을 고치면 다른 쪽은 그대로다. 어느 쪽을
정본으로 삼을지는 아직 정해지지 않았다.

기본 바인딩은 `127.0.0.1`이다. 브라우저 WS는 같은 포트의 localhost/127.0.0.1
Origin만 허용한다. 로컬 비브라우저 클라이언트는 Origin 없이 연결할 수 있다.
임의 웹사이트·다른 PC에 개방한 원격 운용 서버가 아니다.

## 전달 내용

최상위 `device_id`는 실행 시 선택한 개체 프로파일이며, `telemetry.device_id`는
수신한 펌웨어 식별자다. 기존 별칭 검증을 통과한 개체만 들어온다.

| 필드 | 의미 |
| --- | --- |
| `type` | `telemetry` |
| `state`, `escalation` | 현재 Host FSM·대응 단계. 기동 전에는 null |
| `telemetry` | 마지막 수락 패킷의 boot_id·seq·온보드 state·batt_v·dist_cm·imu(pitch/roll/yaw)·last_cmd_age_ms·safety_latched·flags. 미수신이면 null |
| `telemetry_age_ms`, `stale` | 마지막 유효 수신 후 PC 단조 시계 경과. 기존 `safety.link_loss_failsafe_ms` 이상이면 stale |
| `runtime_age_ms`, `runtime_stale` | 마지막 운용 틱 상태 갱신 후 경과. 런타임 중단과 센서 수신 중단을 구분 |
| `link_rtt_ms` | 현재 null. 시계가 다른 로봇 uptime과 PC 시각을 빼서 RTT라고 표시하지 않음 |

유효하지 않은 패킷·타 개체·역전 seq는 기존 수신 규칙으로 폐기하며 최신 값과
수신 시각을 갱신하지 않는다. 새 부팅의 seq=1은 기존 부팅 세션 규칙대로 수락한다.
WS 10Hz는 **표시 갱신률**이다. 동일 seq가 반복되거나 stale인 상태를 새 센서 측정으로
세면 안 된다. stale 상태에서도 마지막 관측은 남지만 정상/안전 판정으로 사용하지 않는다.
전압 범위와 온보드 안전 판정은 변경하지 않았다.

## 지연·종료 처리

운용 스레드는 고정 크기의 상태 사본만 교체하며 WS 송신을 기다리지 않는다.
서버는 별도 스레드/이벤트 루프로 동작하고, 각 클라이언트의 대기 상태는 한 건이다.
느린 화면의 밀린 **상태 표시**는 최신 한 건으로 합치고 횟수를 센다. 센서 입력,
AI 프레임, 명령, 블랙박스 사건은 생략하지 않는다. WS 송신이 1초 안에 끝나지 않으면
그 연결을 닫는다. 최대 16개 연결이며 종료·취소 시 송수신 태스크를 회수한다.

포트 충돌은 기동 실패로 전달한다. 프로그램 종료는 기존 런타임의 ESTOP 정리 후
서버를 종료하는 순서다. WS로 들어온 명령은 거부하며 제어 API는 없다.
영상(4.5.2), 오버라이드/E-Stop API(4.5.3), 이벤트 푸시(4.4.3), 화면(4.6),
실제 RTT 계측과 실물 텔레메트리 연속 운용은 남아 있다.

구현 참고: [FastAPI WebSocket 연결 수명](https://fastapi.tiangolo.com/advanced/websockets/),
[Uvicorn 송신 흐름 제어](https://www.uvicorn.org/server-behavior/).

## 로봇 없는 검증

```powershell
python -m pytest tests/test_dashboard.py -v
```

pytest 안에서 명시적인 시험 패킷을 사용한다. 초기 미수신, IMU 전달, 부팅 전환,
손상·타 개체·역전 폐기, stale 경계, 600틱 명령 동일성, 느린 연결의 메모리 상한,
송신 타임아웃, 16개 연결 정리/재접속, 잘못된 Origin·명령 거부를 확인한다.
추가로 실제 **PC loopback TCP** 서버에 두 WS 클라이언트를 연결해 각각 16개 메시지의
수신 주기를 확인한다. 시험은 로봇 UDP/COM/OTA를 사용하지 않는다.
WBS 완료 상태는 팀 리뷰에서 실제 증거와 함께 반영한다.

2026-09-12 PC 검증: 관제 시험 25건 포함 전체 Python 1,765건 통과,
coverage 87.11% (CI 기준 80%), ruff lint/format·WBS 생성 일치 확인 통과.
실제 loopback TCP 클라이언트 두 개는 각각 16건을 수신했고 측정 구간 평균은
각각 10.00Hz였다. 60초 가상 시계의 600틱에서 관제 연결 전후 생성 명령은 동일했다.
시험 환경의 Starlette TestClient에서 httpx 사용 중단 예정 경고 1건이 있으며
실행 실패는 아니다. 실물 무선·장시간 운용 성능이나 AI 추론 속도 개선을 입증한 결과는 아니다.
