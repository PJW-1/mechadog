# WBS 4.5.1 실측 — 관제 WebSocket 서버 (2026-09-14 · mechdog-01)

기준: 서버 기동, 연결 관리(다중 클라이언트 브로드캐스트), 로봇 텔레메트리를 10 Hz 로 WS 전송.

## 조건

- 로봇 `mechdog-01` · 본체 배터리(충전기 분리) · Wi-Fi `192.168.1.101` · 잠금 상태 유지(안전 해제·순찰 없음)
- `python -m host.runtime --device mechdog-01 --robot-ip 192.168.1.101 --no-vision --dashboard-port 8000`
- WS 클라이언트는 `ws://127.0.0.1:8000/ws/telemetry` 에 붙어 메시지마다 `telemetry.seq`·`boot_id`·`stale` 을 기록했다.
- **10 Hz 는 WS 메시지 수가 아니라 `telemetry.seq` 증가 속도로 판정했다** — 서버는 수신이 끊겨도 마지막 값을
  표시 갱신률로 반복한다(`docs/DASHBOARD.md`).

## 단계 A — 클라이언트 3개 · 30초 (`ws_step-A_3clients_30s.jsonl`)

| 기준 | 결과 |
| :--- | :--- |
| 서버 기동 | `/health` 응답 · 첫 텔레메트리 seq 153 · 배터리 7.70 V |
| 다중 클라이언트 | `/health` clients = 3 · **세 클라이언트가 299건을 똑같이 받음 (299/299)** |
| 로봇 텔레메트리 10 Hz | **seq 증가 10.01/s** · 표시 10.04 Hz · stale 0 · boot_id 1개 |
| 런타임 (`runtime_step-A.log`) | 수락 506 · 폐기 0 · 남의 것 0 · 틱 p95 111 ms |

## 단계 B — 전원 끊김 · 90초 (`ws_step-B_powercycle_90s.jsonl`)

| 확인 | 결과 |
| :--- | :--- |
| 로봇이 꺼져 있을 때 `stale=true` | 기록 시작부터 10.39초 동안 표시 (런타임 IDLE 110틱) |
| 재부팅 후 새 `boot_id` 의 `seq=1` 수락 → `stale=false` | `707bdf755c83619a` seq=1 · 같은 IP 로 재연결 · 이후 seq 건너뜀 0 · 배터리 7.83 V |
| 켜진 상태에서 끊긴 뒤 3초 경계로 `stale` 전환 | 기록 시작 전에 이미 꺼져 있어 **실물로는 못 봤다.** 수신 시각 기준 경계 비교는 단위 시험 `test_stale_boundary_uses_receive_time_not_broadcast` 가 검사한다 |
| 런타임 (`runtime_step-B.log`) | 수락 992 · 폐기 0 · 남의 것 0 · 틱 p95 110 ms |

로그의 `ERROR fsm_transition IDLE→FAILSAFE trigger=ONBOARD_FAILSAFE` 는 로봇이 잠금 상태를 보고해 호스트가 따라간 정상 전이다.
