# 2026-09-22 · 헤드리스 L3 해제(②)와 판정 대기 유예(⑦) 실기

기기 `mechdog-01` · 런타임을 **tty 없이**(백그라운드) 기동 — ② 가 물렸던 그 조건이다.
배터리 7.86 V → 7.63 V. `orientation_applied rot=180` 확인. 로봇 링크 `link_ok: true`.

⚠️ **피험자가 찍힌 프레임은 남기지 않는다.** 아래는 호스트 로그의 수치뿐이다.

## 1판 — ⑦ 판정 대기 유예 (ADR-37)

| 시각 | 사건 |
|---|---|
| 15:49:59 | `ALERT → AUTH_WAIT` (`AUTH_REQUIRED`) · `L1 → L2 unauthenticated_hold` |
| 15:50:00 | `auth_session_dropped reason=track_lost` |
| **15:50:01** | **`voice_auth_deferred granted_ms=10000 cap_ms=10000`** ← 첫 발화 프레임의 `pending` |
| 15:50:04 | `L2 → L0 authenticated` · `AUTH_WAIT → PATROL (AUTH_OK)` · `voice_auth_granted valid_ms=60000` |

- **유예 기전이 실물에서 성립했다.** 마이크가 말을 받은 그 프레임에서 파이프라인이
  `pending` 을 쐈고, 호스트가 `defer_timer` 로 상한 10초를 **꽉** 내줬다.
- 창 밖 발화로 오인되지 않았다 — ⑥ 의 stale 판정을 통과했다는 뜻이다.
- 데몬 스레드 HTTP 가 20ms 프레임 루프를 깨지 않았다(녹음이 끝까지 가 판정에 도달).
- ⚠️ **최악의 경우는 재현되지 않았다.** 판정이 발화 시작 후 **2.65초**만에 왔다
  (앞 회차는 24.3초·18초). 유예가 **실제로 필요한 상황은 아니었다.**
- 침묵한 2판에서는 `voice_auth_deferred` 가 **한 번도** 나오지 않았다 —
  말이 없을 때 끼어들지 않는 것도 함께 확인됐다.

**⑤ 재확인** — `voice_auth_granted valid_ms=60000`(15:50:04) → `unauthenticated_hold`
(15:51:04). **정확히 60초.**

## 2판 — ② 헤드리스 L3 해제

| 시각 | 사건 |
|---|---|
| 15:51:04 | `ALERT → AUTH_WAIT` · L2 주황 |
| **15:51:34** | **`L2 → L3 reason=AUTH_FAILED led=red`** — 진입 후 **정확히 +30.00초** |
| 15:51:44~50 | 순찰 정지 → 수동 경유 `IDLE` |
| **15:55:12** | **`L3 → L0 reason=alarm_confirmed led=blue`** ← 대시보드 «경보 확인 (L3 해제)» |

- **L3 가 3분 38초 동안 유지됐다.** 자동으로 풀리지 않는다는 것도 함께 확인.
- **런타임 재시작 없이 풀렸다.** 이번 수정의 전부가 이 한 줄이다.
- 경보가 없을 때(L0) 누른 것은 `alarm_confirm_ignored level=L0` 로 남고 단계를
  내리지 않았다(15:45:03 · HTTP 스모크).

## 3판 — `reset` 과의 분리 (ADR-26)

사용자가 로봇 전원을 끄자 **링크 상실 25.9초**를 잡아 `L0 → F reason=LINK_LOST` ·
`IDLE → FAILSAFE` 로 갔다(16:0x). 그 F 상태에서 경보 확인을 눌렀다.

- 응답 `accepted: true · state: FAILSAFE`
- 로그 **`alarm_confirm_ignored level=F`**
- **단계는 F, 상태는 `FAILSAFE` 그대로.**

⚠️ **경보 확인이 물리 잠금을 풀지 못한다** — 두 확인을 합치지 않은 이유가 실물에서
그대로 성립했다. 반대 방향(비상정지를 눌렀다 푸는 것으로 경보가 지워지는 것)은
시험 코드가 지킨다(`test_alarm_confirm_does_not_clear_the_failsafe`).

## 남은 것 — 실기에서 확인하지 못한 것

- ⑦ 의 최악 케이스(판정이 창을 넘겨 도착).

## 곁에서 다시 보인 기존 결함

- **브라우저 캐시 때문에 새 버튼이 안 보였다.** 하드 새로고침 전까지 화면이
  `FSM IDLE`·`래치 해제됨` 이라는 **오래된 상태**를 보여 주고 있었다. 정적 파일에
  버전 쿼리나 캐시 헤더가 없다 — **새 결함으로 남긴다.**
- **`ALERT ↔ TRACK` 채터링**(⑩) 그대로 — 인증 직후 7초에 10회 왕복.
- **`auth_session_dropped track_lost`** 가 창 열린 지 0.9초에 발생(③ 자리).
- **`person_found` 블랙박스/음성 피드 스팸**(⑪) 그대로 — 음성 파이프라인 사건 피드
  28건 중 22건이 `person_found` 였다.
- **whisper 환각** — 침묵 구간에 「자막제작에 협조해주신 모든 분들께…」 를 만들어냈다
  (15:52:37). 그 시점 상태가 `AUTH_WAIT` 가 아니라 인증 시도로 세어지지는 않았다.
  ⚠️ **`AUTH_WAIT` 중에 이 환각이 나오면 시도 1회가 날아간다** — `no_speech_prob`
  거름(≥0.6)을 통과한 것이므로 별도로 봐야 한다. **새 결함으로 남긴다.**
