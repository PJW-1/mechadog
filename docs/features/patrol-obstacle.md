# 순찰 중 장애물 대응

로봇이 정면 장애물 앞에서 스스로 멈춘 뒤, 호스트가 물러나며 방향을 틀어 순찰을 잇게 한다. 멈춤 판정은 로봇 펌웨어가 하고([제어 링크와 페일세이프](failsafe.md) 3절), 호스트는 그 보고를 따라 회피 동작만 정한다.
빠져나오지 못하면 정해진 횟수 뒤에 멈춘 채 사람을 기다린다. 같은 장애물 앞에서 계속 흔들어 기어를 상하게 하지 않는다.

순찰 경로는 둘이다. 운용 런타임(`host/runtime.py`)은 FSM 의 `AVOID` 상태로 회피하고, LiDAR 구역 순찰기(`tools/patrol_run.py` 가 돌리는 `PatrolController`)는 초음파 정지 중에는 멈추기만 하고 LiDAR 로 경로를 다시 짠다.

## 판단 흐름

### 1. 운용 런타임의 회피 (`AVOID` 시퀀스)

```mermaid
flowchart TD
  R0["로봇이 state=AVOID 를 새로 보고 · ONBOARD_AVOID"] --> SQ{"호스트 상태가 PATROL 인가?"}
  SQ -->|아니요| KEEP["상태 유지 · 로봇이 전진만 거부"]
  SQ -->|예| AV["AVOID 진입 · 시퀀스를 처음부터"]
  AV --> T0["AVOID 틱 · 10Hz"]
  T0 --> CLRQ{"flags.obstacle 이 참에서 거짓으로 바뀌었나?"}
  CLRQ -->|예 · AVOID_CLEARED| PAT["PATROL 복귀 · 순찰 보행 재개"]
  CLRQ -->|아니요| CAL{"이 기체의 회피 구간이 등록됐나?"}
  CAL -->|아니요 · gait_calibration 미실측| HOLD["정지 유지"]
  CAL -->|예| ATT{"경과 시간이 시도 3회 안인가?"}
  ATT -->|아니요 · 상한 3회| EXH["avoid_exhausted · 정지 유지 · 사람 확인"]
  ATT -->|예| PH["현재 구간 명령 · settle 정지 → reverse_turn 후진 좌선회 → verify 정지"]
  PH -->|다음 틱 · 한 주기 끝나면 다시 settle| T0
  HOLD -->|다음 틱| T0
  EXH -->|다음 틱| T0
```

### 2. LiDAR 구역 순찰기 (`PatrolController`)

```mermaid
flowchart TD
  S0["step · 10Hz"] --> LAT{"로봇이 래치 또는 FAILSAFE 를 보고했나?"}
  LAT -->|예| HALTED["HALTED · 정지 · 사람 해제 대기"]
  LAT -->|아니요| OBA{"flags.obstacle 이 참인가?"}
  OBA -->|예| HOLD["정지 의도 · onboard_obstacle_hold"]
  OBA -->|아니요| ADV["경로 추종 · _advance"]
  ADV -->|다음 스캔| SC["LiDAR 스캔 수신"]
  SC --> EST{"전방 부채꼴 최소 거리가 estop_distance_mm 미만인가?"}
  EST -->|예| ESTOP["ESTOP 즉시 송신 · HALTED"]
  EST -->|아니요| NEW{"새 장애물이 같은 자리에서 연속 2회 잡혔나?"}
  NEW -->|아니요| ADV
  NEW -->|예| MARK["동적 장애물 표시 · 경로만 버림"]
  MARK --> RP{"같은 목표로 경로가 다시 풀리나?"}
  RP -->|예| ADV
  RP -->|아니요| RV{"이 구역 재확인이 3회 미만이고 표시를 지우면 풀리나?"}
  RV -->|예 · 표시 지우고 재계획| ADV
  RV -->|아니요 · 상한 3회| SKIP["zone_unreachable · 이번 사이클 그 구역 건너뜀"]
```

## 판단 기준

| 조건 | 값 | 설정 키 | 근거 |
| :--- | :--- | :--- | :--- |
| 반사 정지 · 해제 | 25cm 미만 연속 2표본 · 30cm 이상 연속 5표본 | `safety.obstacle_stop_cm` (해제는 펌웨어 상수) | [반사 정지 실측](../../field_tests/results/20260917_3.2.6-obstacle-stop/summary.md) |
| 해제 판단 근거 | `flags.obstacle` 의 참→거짓 변화 (플래그가 없는 펌웨어는 `AVOID`→`PATROL` 상태 변화) | 없음 | [ADR-22](../DECISIONS.md#adr-22) |
| 회피 구간 등록 조건 | `forward_mm_per_sec` · `turn_deg_per_sec` 실측값이 있음 | `gait_calibration.*` (기체 프로파일) | [ADR-29](../DECISIONS.md#adr-29) |
| settle · verify 정지 | 각 750ms | `localization.settle_delay_ms` | — |
| 후진 선회 명령 | `MOVE(-60, +20)` · 양수 각도 = 좌선회 | `gait.step_length_mm` · `gait.turn_angle_deg` | [ADR-29](../DECISIONS.md#adr-29) · [ADR-11](../DECISIONS.md#adr-11) |
| 후진 선회 시간 | 30도를 도는 시간과 200mm 물러나는 시간 중 긴 쪽 | `localization.turn_increment_deg` · `gait.reverse_distance_mm` · `gait_calibration.reverse_turn_deg_per_sec` · `reverse_turn_mm_per_sec` | [ADR-29](../DECISIONS.md#adr-29) |
| 후진 선회 미실측 기체 | 후진 200mm 뒤 전진 좌선회 30도 (옛 구간표) | `gait_calibration.reverse_mm_per_sec` (없으면 전진 속도) | [ADR-29](../DECISIONS.md#adr-29) |
| 회피 시도 상한 | 3회 | `fsm.avoid_attempts` | — |
| LiDAR 비상정지 거리 | 전방 ±20° 안 최소 거리 100mm 미만 | `lidar.estop_distance_mm` · `lidar.forward_fan_deg` | — |
| 새 장애물 확정 | 1.5m 안의 빔이 지도가 예상한 거리보다 250mm 이상 가깝고, 0.3m 안 같은 자리에서 연속 2회 | `lidar.new_obstacle_check_radius_mm` · `new_obstacle_margin_mm` · `new_obstacle_confirmations` | — |
| 구역 재확인 상한 | 구역당 사이클마다 3회 | `fsm.avoid_attempts` | — |

## 실패·예외 시 동작

- `AVOID` 는 `PATROL` 에서만 들어간다. `TRACK` 등 다른 상태에서 반사 정지가 걸리면 상태는 그대로이고, 로봇이 전진 명령만 거부한다.
- 해제 보고(`AVOID_CLEARED`)는 회피의 어느 구간에서 오든 그 틱에 `PATROL` 로 돌아간다. 전방이 비었는지는 호스트가 판정하지 않는다.
- 다시 `AVOID` 에 들어오면 시퀀스는 settle 부터 새로 시작한다. 지난 진행을 이어받지 않는다.
- 회피 구간을 만들 실측값이 없는 기체는 `AVOID` 에 시퀀스가 등록되지 않는다. 그 상태의 기본 동작은 정지이고, 기동 로그에 `sequence_unavailable` 이 남는다.
- 시도 3회를 다 쓰면 `avoid_exhausted` 를 한 번 남기고 정지를 계속 보낸다. 그 뒤에도 전방이 비면 `AVOID_CLEARED` 로 순찰에 돌아간다.
- 반사 정지 중 로봇은 후진·선회 명령을 받고 전진 명령은 거부한다(`applied=false`). 그래서 후진 선회는 반사 정지가 걸린 채로도 나간다.
- 순찰기에서 LiDAR 비상정지는 `ESTOP` 이라 로봇이 래치된다. 사람이 해제하고 로봇이 `safety_latched=false` 를 보고해야 경로 계획으로 돌아간다.
- 순찰기에서 동적 장애물 표시는 지도에 쓰지 않는다. 사이클이 끝나면 표시와 재확인 횟수를 모두 지운다.
- 순찰기에서 텔레메트리가 `safety.link_loss_failsafe_ms`(3000ms) 넘게 없으면 `HALTED`, 측위가 `localization.pose_timeout_ms` 넘게 갱신되지 않으면 `LOST` 로 멈춘다. 둘 다 명령 송신은 계속한다.

## 코드와 검증

| 분기 | 코드 위치 | 확인하는 테스트 |
| :--- | :--- | :--- |
| 로봇 `AVOID` 보고 → `ONBOARD_AVOID` | `host/telemetry/receiver.py` 의 `TelemetryReceiver._events_for` | `tests/test_telemetry_receiver.py::test_robot_reporting_avoid_becomes_an_event` |
| `PATROL` 에서만 `AVOID` 로 | `host/behavior/fsm.py` 의 `TRANSITIONS` | `tests/test_fsm.py::test_onboard_states_are_reachable_the_same_way_failsafe_is` (진입 사건이 `ONBOARD_AVOID` 하나뿐인지 본다. 출발 상태가 `PATROL` 하나뿐인지는 시험하지 않는다) |
| 플래그 참→거짓 → `AVOID_CLEARED` | `host/telemetry/receiver.py` 의 `TelemetryReceiver._obstacle_events` | `tests/test_telemetry_receiver.py::test_obstacle_flag_release_clears_avoid` · `tests/test_telemetry_receiver.py::test_missing_obstacle_flag_falls_back_to_the_state_pair` |
| 해제 후 순찰 보행 재개 | `host/behavior/fsm.py` 의 `Behavior.tick` | `tests/test_actions.py::test_leaving_avoid_returns_to_patrol_motion` |
| 재진입 시 처음부터 | `host/behavior/actions.py` 의 `AvoidSequence.restart` | `tests/test_actions.py::test_reentering_avoid_starts_from_the_beginning` |
| 실측값 없으면 정지 유지 | `host/behavior/actions.py` 의 `avoid_phases` · `register_actions` | `tests/test_actions.py::test_avoid_is_not_built_without_calibration` · `tests/test_actions.py::test_missing_calibration_leaves_avoid_stopped` |
| 구간 순서 · 후진 선회 | `host/behavior/actions.py` 의 `avoid_phases` · `AvoidSequence.phase_at` | `tests/test_actions.py::test_avoid_walks_the_phases_in_order` · `tests/test_actions.py::test_reverse_turn_replaces_the_reverse_then_turn_pair` · `tests/test_actions.py::test_reverse_turn_time_satisfies_the_slower_of_the_two_goals` |
| 후진 선회 미실측 기체 | `host/behavior/actions.py` 의 `avoid_phases` | `tests/test_actions.py::test_half_measured_reverse_turn_keeps_the_old_phases` · `tests/test_actions.py::test_reverse_falls_back_to_forward_when_unmeasured` |
| 시도 상한 3회 → 정지 | `host/behavior/actions.py` 의 `AvoidSequence.__call__` | `tests/test_actions.py::test_avoid_retries_the_configured_number_of_times` · `tests/test_actions.py::test_avoid_stops_after_exhausting_attempts` · `tests/test_actions.py::test_exhausted_is_reported` |
| 전진만 거부 | `firmware_mechdog_motion/src/safety_monitor.h` 의 `move_allowed` | `tests/test_safety.py::test_obstacle_blocks_forward_only` |
| 순찰기 래치 보고 우선 | `host/behavior/patrol.py` 의 `PatrolController._guard` | `tests/test_lidar_patrol.py::test_onboard_latch_wins_over_host_plan` |
| 순찰기 반사 정지 중 정지 | `host/behavior/patrol.py` 의 `PatrolController.step` · `SafetyView.obstacle_active` | `tests/test_lidar_patrol.py::test_reported_obstacle_holds_the_walk` · `tests/test_lidar_patrol.py::test_obstacle_release_is_read_from_the_flag` |
| 순찰기 LiDAR 비상정지 | `host/behavior/patrol.py` 의 `PatrolController.guard_scan` | `tests/test_lidar_patrol.py::test_lidar_danger_sends_estop_not_stop` · `tests/test_lidar_patrol.py::test_estop_does_not_wait_for_the_tick` |
| 새 장애물 확정 · 재계획 · 재확인 상한 | `host/behavior/patrol.py` 의 `PatrolController._check_new_obstacle` · `PatrolController._replan` | 시험 없음. `tests/test_lidar_patrol.py::test_dynamic_obstacle_does_not_change_the_map` 는 표시가 지도에 쓰이지 않는 것만 본다 |
| 막힌 구역이 사이클을 끝내지 않음 | `host/behavior/patrol.py` 의 `PatrolController._replan` | `tests/test_lidar_patrol.py::test_blocked_zone_does_not_end_the_cycle_early` |

실측 기록

- [온보드 근거리 반사 정지 — 전진 거부 · 후진 허용 · 해제](../../field_tests/results/20260917_3.2.6-obstacle-stop/summary.md)
- 회피 시퀀스 전체(후진 선회 → 해제 → 순찰 재개)를 실기에서 잰 기록은 아직 없다.
