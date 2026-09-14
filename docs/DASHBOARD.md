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
- `ws://127.0.0.1:8000/ws/vision`: 검출 오버레이 — JPEG 와 박스를 한 메시지로 (아래 절). 카메라가 있을 때만 열린다.
- `http://127.0.0.1:8000/docs`: HTTP API 확인.
- `http://127.0.0.1:8000/#dashboard`: 관제 화면 (아래 절).
- `http://127.0.0.1:8000/live`: 최소 실기 화면 — 카메라 · 배터리/거리 · E-STOP · 안전 해제 · 수동 제어.

## 관제 화면

화면은 **`host/dashboard/static/`** 에 있고 서버가 빌드 없이 그대로 내보낸다(WBS `4.6.x` 산출물 위치).
2026-09-14 에 `web/design-prototype` 에서 옮겼다. 설계 검토용 프로토타입을 서버가 그 폴더째 내보내던
구조였고, three.js 를 `node_modules` 에 기대서 **설치를 빠뜨리면 app.js 가 첫 import 에서 실패해 화면
조작이 통째로 죽었다** — 3D 만 비는 것이 아니었다. 이제 three.js 를 `static/vendor/` 에 함께 싣는다.

| 파일 | 역할 |
| --- | --- |
| `index.html`, `styles.css` | 관제 화면과 공통 스타일 |
| `panels.js`, `panels.css` | 작업 페이지와 수동 조작 |
| `app.js`, `operations.js` | 화면 연결, 상태·검증 규칙 |
| `robot-link.js` | 명령 API 연결 (E-Stop · 수동 · 조이스틱) |
| `scene.js`, `scene-materials.js`, `robot-view.js` | 공장·로봇 3D, 장치 상세 |
| `factory-layout.json` | 표시용 공장 배치 (실행 필수) |
| `icons.js`, `webmcp.js` | 아이콘, 지원 브라우저의 페이지 도구 |
| `live.html` | 최소 실기 화면 (`/live`) |
| `vendor/` | three.js 0.186.0 — 화면이 실제로 불러오는 18개 파일과 `THREE-LICENSE.txt` |

⚠️ **실데이터와 이어진 것은 명령(E-Stop · 수동 · 조이스틱)과 카메라뿐이다.** 서버가 이 화면을 내보내면
`/health` 로 서버를 알아보고 명령 경로를 붙이므로, **수동 조작은 실제 로봇을 움직인다.** 텔레메트리
게이지 · 검출 박스 · 사건 · 지도 · 위치는 아직 예시 데이터이며 화면이 "예시"로 표시한다. 요구사항 대조와
남은 연결은 [DASHBOARD_FEATURES](DASHBOARD_FEATURES.md).

⚠️ `styles.css` 는 글꼴을 Google Fonts 에서 받는다. 인터넷이 없으면 기본 글꼴로 보인다.

### 개발 시험

런타임에는 설치가 필요 없다. 화면 코드의 Node 시험만 `host/dashboard/` 에서 돈다.

```powershell
cd host/dashboard
npm ci          # 시험용 jsdom · three
npm run check   # JS 문법 + static/vendor 가 잠긴 three 버전과 같은지
npm test        # 화면 동작 + 모든 import 가 static/ 안에서 해결되는지
```

three 버전을 올리면 `package.json` 을 고친 뒤 `npm ci` → `npm run vendor` 로 `static/vendor` 를 다시
채운다. 싣는 파일 목록은 `scripts/static-check.mjs` 에 있고, 모자라면 `npm test` 의 import 해석 시험이
실패한다.

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

## 검출 오버레이 — `/ws/vision` (WBS 4.5.2)

비전 워커의 추론 결과 하나를 **바이너리 메시지 하나**로 보낸다.

```
[헤더 길이 uint32 big-endian][UTF-8 JSON 헤더][JPEG]
```

```json
{"type":"vision","frame_seq":1413,"width":640,"height":480,"completed_ms":...,
 "detections":[{"label":"person","score":0.903,"box":[22.8,2.8,527.2,476.7]}],
 "tracks":[{"track_id":2,"score":0.903,"box":[22.8,2.8,527.2,476.7]}]}
```

- ⚠️ **JPEG 는 박스를 계산한 바로 그 프레임이다.** 카메라 스트림(`/camera/stream`)에 박스를 따로 얹으면
  추론 지연만큼 박스가 다른 장면 위에 그려진다.
- ⚠️ **헤더와 JPEG 를 두 메시지로 나누지 않는다.** 송신 시간 초과로 둘째가 취소되면 다음 JPEG 가 앞
  헤더와 짝지어져 박스가 엉뚱한 사진에 붙는다.
- 박스는 원본 픽셀 좌표 `[x1, y1, x2, y2]` 다. 화면 크기에 맞출 때 `width`·`height` 로 늘린다. 필드
  이름은 블랙박스 기록과 같다.
- `detections` 는 검출기의 모든 클래스, `tracks` 는 추적 중인 사람이다.
- **새 추론이 나왔을 때만** 보내고, 느린 연결에는 최신 한 장만 남긴다. **새로 붙은 연결은 마지막 프레임부터**
  받는다 — 카메라가 멈춰 있어도 빈 화면이 되지 않고, 낡았는지는 `completed_ms` 로 가린다.
- `/health` 의 `vision_clients` 가 연결 수다. 카메라 없이 띄우면(`--no-vision`) `null` 이고 채널이 열리지 않는다.
- 출처 검사 · 연결 상한 · 느린 연결 차단은 텔레메트리 채널과 같은 코드를 쓴다.

**실기 확인 (2026-09-14 · XIAO OV3660 VGA)** — 30초 294프레임 · 초당 9.8 · JPEG 형식 오류 0 · 화면 밖
박스 0 · 사람 추적 박스가 294프레임 전부에 있었다. 받은 메시지의 JPEG 위에 같은 메시지의 박스를 그려
사람과 모니터 위치에 맞게 붙는 것을 확인했다. 원자료
`TEST_MECHDOG/results/20260914-185200_4.5.2-vision-overlay/` (사람이 찍힌 사진은 싣지 않았다).

## 지연·종료 처리

운용 스레드는 고정 크기의 상태 사본만 교체하며 WS 송신을 기다리지 않는다.
서버는 별도 스레드/이벤트 루프로 동작하고, 각 클라이언트의 대기 상태는 한 건이다.
느린 화면의 밀린 **상태 표시**는 최신 한 건으로 합치고 횟수를 센다. 센서 입력,
AI 프레임, 명령, 블랙박스 사건은 생략하지 않는다. WS 송신이 1초 안에 끝나지 않으면
그 연결을 닫는다. 최대 16개 연결이며 종료·취소 시 송수신 태스크를 회수한다.

포트 충돌은 기동 실패로 전달한다. 프로그램 종료는 기존 런타임의 ESTOP 정리 후
서버를 종료하는 순서다. WS로 들어온 명령은 거부한다. 제어는 HTTP 명령 API(`/api/command/*` · 4.5.3)로만 받는다.
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
