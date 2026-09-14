# WBS 4.6.3 실측 — 관제 화면 수동 조작·E-Stop (2026-09-14 · mechdog-01)

기준: 조이스틱 조작이 실제 로봇에 반영될 것, E-Stop 버튼이 상시 노출될 것.

## 조건

원자료는 저장소가 `*.jsonl` 을 무시하므로 `.txt` 로 둔다. `ws_*.txt` 는 JSON Lines 다.

- 로봇 `mechdog-01` · 본체 배터리 · Wi-Fi `192.168.1.101`(ARP MAC `3c-8a-1f-33-32-08` 으로 개체 확인)
- `python -m host.runtime --device mechdog-01 --robot-ip 192.168.1.101 --no-vision --dashboard-port 8000`
  ⚠️ 단계 A 는 `--reset-on-start` 를 붙였는데 **해제 전문이 로봇 부팅과 경합해 유실됐다** — 이 명령은
  1회성이라 재시도가 없다. 단계 B·C 는 텔레메트리가 흐르기 시작한 뒤 `/api/command/reset` 으로 풀었다.
- 조작 화면은 `http://127.0.0.1:8000/#dashboard`. 카메라는 4.6.3 DoD 밖이라 `--no-vision` 으로 뺐다.
- 기록은 `ws://127.0.0.1:8000/ws/telemetry` 에 붙은 클라이언트 1개가 수신 시각과 함께 원문을 그대로 적었다.
  **명령 API 는 로그를 남기지 않으므로**(`server.py` 의 `/api/command/*` 에 로깅 없음) 조작의 증거는 텔레메트리 쪽에서 나온다.
- 방향은 전진·후진·좌회전·우회전을 각각 약 3초씩, 사이에 정지를 두고 조작했다.

## 결과 요약

| DoD | 결과 |
| :--- | :--- |
| 조이스틱 조작 반영 | **충족** — 제어권 요청이 서버 `MANUAL` 로 들어갔고(26.7초 유지) 조작자가 4방향 모두 실제 보행을 육안 확인. `Space` 정지도 함께 확인 |
| E-Stop 버튼 상시 노출 | **충족** — 화면 우측 상단 버튼을 눌러 로봇이 `FAILSAFE` · `safety_latched=true` 로 잠기는 것을 확인(단계 C). 단 **대응 단계가 `F` 로 올라가지 않는다**(결함 ②) |

전 구간 텔레메트리: **수락 5,557 · 송신 5,825 · 폐기 0 · 타 개체 0**.

## 단계 A — 1차 시도 (`boot c810e10c09df04ad`)

| 시각 | 사건 |
| :--- | :--- |
| 21:23:53 | `RESET_SAFE` 수락 → `FAILSAFE → IDLE` (`RESET_CONFIRMED`) |
| 21:24:53 | 로봇이 `MANUAL` 보고 — 제어권 진입 |
| 21:25:03 | **`MANUAL → FAILSAFE` (`LINK_LOST`)** — 수동 조작 10초 만에 링크 두절 |
| 21:25:25 | 로봇이 `FAILSAFE` · `safety_latched=true` 보고, 이후 재부팅 |

⚠️ **보행 중 전압 강하가 링크 두절과 같이 왔다.** 이 구간의 `batt_v` 는 **최저 7.09V**(정지 중 7.26~7.59V)로
경고 임계 7.0V 에 근접했다. 재충전 후의 2차 시도에서는 같은 조작에서 최저 7.48V 였고 두절이 없었다.
**단정하지 않는다** — 전압·링크 중 어느 쪽이 원인인지 이 기록만으로는 가를 수 없다. 다만 보행 부하에서
재현되는지 확인할 값이며, `2.2.2`(배터리 지속시간)와 RISK-08 에 연결된다.

## 단계 B — 2차 시도 (`boot e93169d45ca14ee7`)

| 시각 | 사건 |
| :--- | :--- |
| 21:26:31 | `RESET_SAFE` 수락 → `FAILSAFE → IDLE` |
| 21:27:17 | 로봇이 `MANUAL` 보고 · 대시보드 `state=MANUAL` |
| 21:27:44 | `MANUAL → IDLE` — 수동 종료 |

| 확인 | 결과 |
| :--- | :--- |
| 수동 제어권이 서버까지 도달 | `MANUAL` 268건 · **26.7초** 유지 |
| 명령 수신 지연 | `last_cmd_age_ms` **p95 18ms** (10Hz 고정 송신 유지) |
| 배터리 | 7.48 ~ 7.94V · 경고 임계 미도달 |
| 4방향 보행 | **조작자 육안 확인** — 전진·후진·좌회전·우회전 모두 반영 |

⚠️ **방향별 각속도는 이 기록으로 확정하지 않는다.** 온보드 IMU 의 10Hz `yaw` 는 구간 잡음이
±10~30°/s 로, 확인하려는 선회율(좌 6.8 / 우 3.56 °/s · `2.2.3`)보다 크다. `2.2.3` 이 진폭 실측에
폰 IMU 99.4Hz 를 쓴 것과 같은 이유다. **방향 정합은 조작자 관찰이 근거이고 수치는 `2.2.3` 소관이다.**

## 단계 C — E-Stop 버튼 확인 (같은 부팅 `e93169d45ca14ee7` · `ws_estop.txt`)

관제 화면 우측 상단의 빨간 E-Stop 버튼을 실제로 눌렀다. 연결된 상태이므로 모달 없이 한 번에 나간다.

```
+  0.0s  host=FAILSAFE  esc=F   robot=FAILSAFE  latched=True    <- 기동 시 온보드 래치를 따라감
+  6.4s  host=IDLE      esc=L0  robot=IDLE      latched=False   <- RESET_SAFE
+ 32.3s  host=MANUAL    esc=L0  robot=IDLE      latched=False   <- 수동 제어권
+ 32.4s  host=MANUAL    esc=L0  robot=MANUAL    latched=False
+ 34.5s  host=FAILSAFE  esc=L0  robot=FAILSAFE  latched=True    <- E-Stop 버튼
```

| 확인 | 결과 |
| :--- | :--- |
| 버튼을 눌러 로봇이 잠기는가 | **충족** — 로봇이 `FAILSAFE` · `safety_latched=true` 보고 |
| 호스트가 `FAILSAFE` 로 가는가 | **충족** |
| 대응 단계가 `F` 로 가는가 | **미충족** — `L0` 에 머물렀다 (아래 ②) |

⚠️ **같은 기록 안에 정상 사례와 결함 사례가 나란히 있다.** `+0.0s` 의 잠김은 온보드가 스스로 건 것이라
`ONBOARD_FAILSAFE` 경로를 타 `esc=F`(눈 LED 흰색)까지 올라갔다. `+34.5s` 의 잠김은 E-Stop 버튼이라
**같은 `FAILSAFE` 인데 `esc=L0`(파랑)** 이다. 상태는 같고 표현만 다르다.

## 발견 — 결함 2건

### ① 로봇이 다시 잠겼는데 호스트가 따라가지 않는다

같은 부팅 세션 안에서 로봇이 보고한 온보드 상태다.

```
+  0.0s  robot=FAILSAFE  latched=True   host=FAILSAFE   <- 따라감
+ 65.6s  robot=IDLE      latched=False  host=IDLE
+111.9s  robot=MANUAL    latched=False  host=MANUAL
+138.7s  robot=IDLE      latched=False  host=IDLE
+159.2s  robot=FAILSAFE  latched=True   host=IDLE       <- 따라가지 않음
```

마지막 줄 이후 **호스트는 `IDLE` · 에스컬레이션 `L0`(파랑) 로 남았다.** 링크는 정상이었다(`last_cmd_age` 11ms).
즉 **로봇은 잠겨 있는데 관제 화면은 정상 순찰 가능 상태로 보인다.**

원인은 `host/telemetry/receiver.py` 의 `_events_for` 다. 호스트 전용 상태(`IDLE`·`MANUAL`)는
`ONBOARD_STATES` 가 아니라 **조기 반환하면서 `_last_onboard` 를 갱신하지 않는다.** 그래서 기억은
`FAILSAFE` 에 머물고, 로봇이 `IDLE` 을 거쳐 다시 `FAILSAFE` 로 갔을 때 `previous == reading.state` 가
되어 **엣지로 보지 않아 `ONBOARD_FAILSAFE` 를 만들지 않는다.**

기억을 지우지 않는 것은 의도였다 — 코드 주석이 *"기억하면 `AVOID → ALERT → PATROL` 같은 경로에서
회복을 놓친다"* 고 적어 두었다. 그 의도가 반대 방향에서 **재진입을 놓치는** 구멍을 만들었다.
아키텍처 설계 규칙 ③(온보드 반사는 보고를 받아 따라간다)에 어긋난다.

### ② 대시보드 명령의 전이가 로그에도 에스컬레이션에도 남지 않는다

`host/dashboard/commands.py` 의 `estop()`·`manual_on()`·`manual_off()` 는 `behavior.event()` 를
**직접** 부른다. `runtime._apply()` 를 거치지 않으므로 두 가지가 함께 빠진다.

| 빠지는 것 | 결과 |
| :--- | :--- |
| `_log_transition()` | `fsm_transition` 레코드가 없고, 이후 레코드의 `state` 가 실제와 어긋난다 |
| `_escalation.note_event()` | 대응 단계가 갱신되지 않는다 |

실측으로 둘 다 확인했다.

- 26.7초 동안 대시보드는 `state=MANUAL` 을 내보냈지만, 같은 시각 로그 레코드의 `state` 는 `IDLE` 이고
  `IDLE→MANUAL→IDLE` 전이가 **로그에 한 줄도 없다.**
- **단계 C 에서 실제 버튼으로 재현했다.** 로봇은 잠겼고 호스트는 `FAILSAFE` 로 갔는데
  `fsm_transition`·`escalation` 레코드가 **둘 다 없었고** `escalation` 은 `L0` 에 머물렀다.
  같은 기록의 `+0.0s`(온보드 자체 래치)는 `esc=F` 까지 정상으로 올라갔으므로, **차이를 만드는 것은
  잠긴 이유가 아니라 어느 경로로 들어왔는가**다.

⚠️ **에스컬레이션이 안 올라가면 눈 LED 가 `white` 로 가지 않는다**(FR-10.4 · 아키텍처 3.1 표의 `F` 행).
즉 비상정지를 눌러도 **로봇의 상태 표시등이 바뀌지 않는다.**

`_apply()` 의 독스트링이 바로 이 함정을 경고하고 있다 — *"이 경로를 우회하면 로그의 `state` 가 실제와
어긋난다"*, `start_patrol()` 에서 한 번 겪었다고 적혀 있다. 대시보드 명령이 같은 우회를 하고 있다.

### ③ E-Stop 키보드 단축키가 비상정지를 보내지 않는다

시험 중 호스트가 `FAILSAFE` 로 가지 않은 것은 결함이 아니었다. **`Space` 는 E-Stop 이 아니라
일반 정지다** — `stopManualInput` → `store.stop()` → `link.drive('STOP')` 이며 `move(0,0)` 에
해당한다. 래치를 걸지 않고 FSM 상태도 바꾸지 않는다. `Escape` 도 같다. 화면 안내도
*"`W A S D` 이동 · `Space` 정지"* 로 정확히 적혀 있다. 즉 **관측된 동작이 정상이다.**

그런데 확인 과정에서 별개의 결함이 나왔다. `FR-4.4` 는 E-Stop 에 **키보드 단축키**를 함께
제공할 것을 요구하는데, 유일한 단축키 `Shift+E` 는 **연결 여부와 무관하게 안내 모달만 연다**
(`app.js` 의 `openStopDialog()`). 실제 전송은 모달 안에서 한 번 더 눌러야 일어난다.

같은 파일의 버튼 핸들러는 정반대 원칙을 명시하고 있다 — *"연결돼 있으면 비상정지는 한 번 눌러
바로 나간다. 모달을 한 단계 끼우면 급할 때 그만큼 늦고, 그 모달은 「장비가 연결되지 않았어요」
라고 거짓을 말한다."* **버튼은 이 원칙을 지키고 단축키는 지키지 않는다.**

⚠️ 곁들여 확인한 것 둘.

- `/api/command/estop` 을 직접 호출하면 **즉시 `IDLE → FAILSAFE`** 로 간다 — 경로 자체는 정상이다.
- 서버에 **접근 로그가 없어** POST 도달 여부를 사후에 확인할 수단이 없다. 웹의 `operations.estop()`
  은 `this.estop=true` 를 **전송 성공 여부와 무관하게** 세우므로, 전송이 실패해도 버튼은 잠긴
  모양으로 보인다(실패는 로그 패널에만 남는다). 다음 확인 때 이 둘이 걸림돌이 된다.

## 판정

- **DoD 두 항목(조이스틱 조작 반영 · E-Stop 버튼 상시 노출)이 모두 실기로 충족**됐다.
- 결함 ①②는 DoD 문구 밖이지만 **관제 화면이 로봇 상태를 틀리게 표시하는 문제**다.
  ①은 잠긴 로봇을 정상으로 보이게 하고, ②는 `FR-10.4` 눈 LED 와 M3 DoD(전 상태 전이의
  구조화 로그 기록)에 직접 걸린다. 4.6.3 과 함께 처리하는 것이 맞다.
- 결함 ③은 `FR-4.4` 의 **키보드 단축키 요구**에 걸린다.

## 수정 (2026-09-14)

세 결함을 모두 고쳤다. 전 시험 통과 — Python 2,090건 · coverage 86.36% · 웹 91건 · ruff.

| 결함 | 고친 곳 | 회귀 시험 |
| :--- | :--- | :--- |
| ① 재진입을 놓침 | `host/telemetry/receiver.py` — 호스트 전용 상태를 지나간 **사실**을 `_left_onboard` 에 남겨, 같은 값으로 돌아와도 엣지로 본다. **값은 여전히 기억하지 않으므로** `AVOID → ALERT → PATROL` 회복은 그대로다 | `test_relatching_after_a_host_only_state_is_still_an_event` · `test_returning_to_the_same_onboard_state_does_not_repeat_afterwards` |
| ② `_apply` 우회 | `host/runtime.py` 에 `apply_external()` 진입점을 두고 `CommandService(apply_event=...)` 로 주입. `estop`·`manual_on`·`manual_off` 가 그 경로로 사건을 넣는다 — 대응 단계와 전이 로그가 함께 따라온다 | `test_commands_route_events_through_the_given_hook` · `test_runtime_wires_the_apply_hook_so_escalation_follows_estop` |
| ③ `Shift+E` 가 모달만 엶 | `host/dashboard/static/app.js` — 단축키가 버튼과 같은 `onEstopPressed()` 를 탄다. 연결됐으면 바로 나가고, 보낼 곳이 없을 때만 안내를 띄운다 | 웹 시험 2건 (연결 시 즉시 전송 · 미연결 시 안내) |

⚠️ **②의 회귀 시험이 실기 증상을 그대로 물린다** — 런타임에 붙인 `CommandService` 로 `estop()` 을
부르면 단계가 `L0 → F` 로 올라가는지 본다. 고치기 전에는 `L0` 에 머물렀다.

## 원자료

| 파일 | 내용 |
| :--- | :--- |
| `ws_manual.txt` | 단계 A·B — `/ws/telemetry` 스냅샷 4,984건 (수신 시각 포함). 조작 구간은 앞의 2,436건이고, 그 뒤는 결함 ① 이 지속되는 구간과 `/api/command/estop` 직접 호출 확인이다 |
| `runtime_console.txt` | 단계 A·B 런타임 콘솔 출력 |
| `ws_estop.txt` | 단계 C — E-Stop 버튼 확인 구간 |
| `runtime_console_estop.txt` | 단계 C 런타임 콘솔 출력 |

호스트 구조화 로그는 `logs/mechdog-01.jsonl` 의 21:22:57 이후 구간이다(저장소 제외 대상).

⚠️ **`ws_*.txt` 는 전체 페이로드가 아니라 투영본이다** — 이 문서가 인용하는 필드만 남겼다
(`recv_ms`·`state`·`escalation`·`stale`·`boot_id`·`seq`·`robot_state`·`safety_latched`·`batt_v`·
`dist_cm`·`yaw`·`last_cmd_age_ms`). 4.5.1 이 `seq`·`boot_id`·`stale` 만 적은 것과 같은 이유다 —
원문 그대로면 같은 길이에 4배가 넘는다. `ws_manual.txt` 끝의 상태 변화 없는 대기 구간은 잘랐다.
