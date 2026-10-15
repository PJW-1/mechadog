# 기록 세션 재생 — 2026-10-06

`--record-dir` 로 남긴 세션을 `Runtime` 에 다시 넣어 같은 명령·FSM 전이·경보 단계가 나오는지 맞춰 본
기록이다. 도구는 `tools/ops/replay_session.py`, 사용법은 `tools/README.md` 의 «기록 세션 재생» 절이다.
**로봇·XIAO 에는 접속하지 않았다** — 소켓은 가짜, 시계는 기록 시각을 따르는 가짜 시계다.

## 실기 기록 — 재생할 대상이 없었다

재생 입력(`events.jsonl` + `manifest.json`)이 있는 실기 세션을 찾았지만 없었다.

- 저장소에 커밋된 `events.jsonl` 없음. `*.jsonl` 은 `.gitignore` 대상이라 원래 커밋되지 않는다.
- 메인 작업 트리에 남은 `20260923_escalation-warning/events.jsonl` 은 런타임 사건 형식
  (`escalation_changed` 등)이라 재생 입력이 아니다. 세션 기록기 manifest 도 없다.

그래서 **실기 세션의 일치율과 불일치 원인은 아직 없다.** 다음 실기 때 런타임을 `--record-dir` 로
띄우면 바로 재생할 수 있다.

## 합성 세션 결과

시험(`tests/test_session_replay.py::_record_session`)과 같은 3초 세션이다. 순찰 시작 → 1.2초에 사람
확정(ALERT) → 2.5초에 관제 ESTOP. 실제 `Runtime.serve` 로 기록한 뒤 CLI 로 재생했다.

| 재생 | 명령 일치 | FSM 전이 | 경보 단계 |
|---|---|---|---|
| 기록 그대로 | 42/42 (100%) | 3/3 | 2/2 |
| `--set network.cmd_rate_hz=5` (이 설정이었다면) | 27/42 (64.3%), 첫 불일치 0.5초의 MOVE | 3/3 | 2/2 |
| 입력 사건(`operator`·`runtime_begin`)을 지운 옛 형식 흉내 | 9/42 (21.4%), 재생 38건 | 0/3 | 1/2 |

- 같은 입력을 두 번 재생하면 JSON 보고가 같다(`test_replay_is_deterministic`).
- 송신 주기를 바꾸면 FSM·경보 단계는 그대로이고 명령 수만 줄어든다. 판단은 같고 전송만 성기게 된다는 뜻이다.

## 재생이 짚은 원인 — 옛 기록에는 관제 입력이 없었다

옛 형식 흉내의 첫 불일치는 기동 시각의 `STATE` 다.

- 기록: `{"state":"PATROL","type":"STATE"}` 뒤에 MOVE 가 이어진다.
- 재생: `{"state":"IDLE","type":"STATE"}` 에서 멈춘다. FSM 전이 `IDLE->PATROL (START_PATROL)` 이 나오지 않는다.

대시보드·콘솔 스레드에서 들어온 `ask_patrol` 과 ESTOP 이 기록에 남지 않아, 재생된 런타임은 IDLE 에
머문다. 그래서 기록기에 다음 사건을 더했다.

- `runtime_begin`: 세션 개시 시각. 재생의 첫 틱을 여기에 맞춘다.
- `operator`: 관제 입력 11곳(`set_mode`·`ask_alarm_confirm`·`ask_reset`·`ask_goto`·`ask_locate_zone`·
  `apply_external`·`note_voice_listening`·`note_voice_auth`·`ask_patrol`·`send_emergency_stop`·`send_immediate`).
- `escalation`: 경보 단계가 바뀐 시각과 단계.
- `vision` 에 판정 전체(`sighting`·`tracks`·`fallen`·`markers`·`ppe`·`hazard`). 기존 키는 그대로다.

## 재현 한계

- LiDAR 길 찾기(`scan`)는 넣지 않는다. `--lidar-device` 세션의 PATROL 중 MOVE 는 다르게 나온다.
- VLM 판독 결과는 기록되지 않아 «판독기 없음»으로 재생한다.
- 대시보드가 `Commander` 를 직접 만진 조종은 입력이 남지 않는다.
- `send_emergency_stop` 은 대시보드 ESTOP 과 LiDAR 위험 정지 콜백이 같은 `operator` 이름으로 기록된다.
- `--reset-on-start` 는 manifest 의 argv 에서 복원한다.
- 지금 설정의 해시가 manifest 의 `config_sha256` 와 다르면 보고에 알린다.
