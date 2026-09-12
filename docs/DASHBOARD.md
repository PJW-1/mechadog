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
