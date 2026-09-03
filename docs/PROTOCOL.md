# 통신 프로토콜 정본

> **이 문서가 정본이다.** 송신(Python)과 수신(C++ 펌웨어)은 이 문서만 보고 구현한다.
> 스키마는 팀장이 결정하여 전파한다. 합의 대상이 아니다.
>
> 상세 배경과 설계 근거는 [PRD FR-5](PRD_Physical_AI_Guard_Robot.md) 참조.

---

## 1. 전송 계층

| 링크 | 방향 | 프로토콜 | 주기 |
| :--- | :--- | :--- | :--- |
| 제어 명령 | Host PC → MechDog ESP32 | UDP | **10 Hz 고정** |
| 텔레메트리 | MechDog ESP32 → Host PC | UDP | 10 Hz |
| 영상 | XIAO → Host PC | HTTP MJPEG | 15 fps |

> 제어 명령은 **변화가 없어도 계속 보낸다.** 수신측 타임아웃(300ms)을 갱신하는 것이
> 곧 "링크가 살아 있다"는 신호이기 때문이다. 별도 하트비트를 두지 않는다.

---

## 2. 제어 명령 — 7종

모든 메시지는 **한 줄 JSON**이며 `seq` · `ts` · `type` 3개 필드를 공통 필수로 갖는다.

```json
{"seq": 1234, "ts": 1756800000123, "type": "MOVE",   "step": 60, "angle": 12}
{"seq": 1235, "ts": 1756800000223, "type": "POSE",   "pitch": 15, "roll": 0, "height": 0, "dur": 300}
{"seq": 1236, "ts": 1756800000323, "type": "GAIT",   "lift_time": 120, "ground_time": 180, "height": 25}
{"seq": 1237, "ts": 1756800000423, "type": "STOP"}
{"seq": 1238, "ts": 1756800000523, "type": "ACTION", "id": 7}
{"seq": 1239, "ts": 1756800000623, "type": "LED",    "color": "red", "blink_hz": 2}
{"seq": 1240, "ts": 1756800000723, "type": "SOUND",  "phrase_id": 181}
```

### 공통 필드

| 필드 | 타입 | 의미 |
| :--- | :--- | :--- |
| `seq` | int | 송신측 단조 증가. 역전·중복 판정 기준 |
| `ts` | int | Host PC 기준 epoch **밀리초** (초 아님) |
| `type` | str | 대문자 고정 |

### 타입별 필드

| type | 필수 필드 | 범위 | HW_MechDog 매핑 |
| :--- | :--- | :--- | :--- |
| `MOVE` | `step`, `angle` | −100~100 mm / −30~30 deg | `move(step, angle)` |
| `POSE` | `pitch`, `roll`, `height`, `dur` | 라이브러리 허용 범위 / dur ms | `transform(pose, dur)` |
| `GAIT` | `lift_time`, `ground_time`, `height` | ms / ms / mm | `set_gait_params(...)` |
| `STOP` | — | — | `move(0, 0)` |
| `ACTION` | `id` | 0~15 | 내장 액션 그룹 |
| `LED` | `color`, `blink_hz` | `config.escalation.led` 색상명 / 0 = 상시점등 | 눈 LED |
| `SOUND` | `phrase_id` | WonderEcho 사전 등록 문구 ID | 문구 재생 |

> `MOVE` 는 **호(arc) 조향**이다. 제자리 회전은 지원하지 않는다 (DR-11).
> `SOUND` 는 사전 등록된 문구만 재생한다. 실시간 TTS 가 아니다.

---

## 3. 수신측 검증 규칙 — 4개

이 4개는 **펌웨어에서 반드시 이 순서로** 구현한다.

| # | 규칙 | 이유 |
| :-- | :--- | :--- |
| ① | `seq` 가 마지막 수신값 이하면 **폐기** | UDP 순서 뒤바뀜 대응 (FR-1.6) |
| ② | 필드가 범위를 벗어나면 **클램핑** (폐기 아님) | 명령이 조용히 사라지는 것보다 낫다 |
| ③ | 파싱 실패 시 폐기. **타임아웃 카운터를 갱신하지 않는다** | 깨진 패킷을 "살아 있음"으로 오해하면 페일세이프가 안 걸린다 |
| ④ | 모르는 `type` 은 **폐기 + WARN 로그** | 아래 확장 정책의 근거 |

---

## 4. 스키마 확장 정책

**④ 덕분에 새 타입 추가는 항상 하위 호환이다.** 받는 쪽이 모르는 타입을 안전하게
무시하므로, 한쪽이 먼저 보내기 시작해도 기존 동작이 깨지지 않는다. 다른 쪽이
나중에 핸들러를 추가하면 그 시점부터 동작한다.

| 유형 | 예 | 절차 |
| :--- | :--- | :--- |
| **하위 호환 (additive)** | 새 `type` 추가, 옵션 필드 추가 | **자유롭게 진행.** PR 본문에 한 줄 기재 |
| **파괴적 (breaking)** | 필드명·단위·의미·범위 변경, 타입 삭제 | **팀장에게 알리고** 송·수신·픽스처를 함께 수정 |

> 필드를 **추가**하는 건 언제든 해도 된다. **바꾸거나 지우는 것**만 조심하면 된다.

---

## 5. 텔레메트리 (ESP32 → Host PC)

```json
{
  "seq": 88, "ts": 1756800000500,
  "device_id": "mechdog-b",
  "state": "PATROL",
  "dist_cm": 47,
  "imu": {"pitch": 1.2, "roll": -0.4, "yaw": 183.5},
  "batt_v": 7.62,
  "last_cmd_age_ms": 34,
  "flags": {"lowbatt": false, "tipped": false, "link_ok": true}
}
```

> **`device_id` 는 필수다.** 다중 개체를 돌릴 때 송신자를 IP 로 구분하면 안 된다. DHCP 로 바뀔 수 있고, 호스트가 컨테이너 안이면 출처가 게이트웨이로 보인다 (DR-17).
>
> ESP32 는 파일 로깅을 하지 않는다. **텔레메트리가 곧 로그다.**

### 수신측 검증 규칙

제어 명령과 대칭이다. **송신은 펌웨어(L1·L2), 수신은 호스트(팀장)** 이므로 같은 인터페이스 위험을 갖는다.

| # | 규칙 |
| :-- | :--- |
| ① | `seq` 역전·중복 폐기 |
| ② | `seq` · `device_id` · `state` · `imu` · `flags` 중 하나라도 없으면 폐기 |
| ③ | **모르는 `state` 는 폐기 + WARN** — 상태 추가를 하위 호환으로 만든다 |
| ④ | `batt_v` 가 2S 리튬 물리 범위(6.0~8.4V) 밖이거나 `dist_cm` 이 음수면 폐기 |

### 정본 픽스처

| 파일 | 역할 |
| :--- | :--- |
| `tests/fixtures/telemetry_samples.jsonl` | 유효 레코드 — **FSM 상태 8종 전부 + 안전 임계 경계값** |
| `tests/fixtures/telemetry_invalid.jsonl` | 폐기 대상 + 기대 동작 |
| `tests/test_telemetry_fixtures.py` | 스키마 + **`config.yaml` 안전 임계와의 교차 검증** |

> **교차 검증이 핵심이다.** 픽스처에 `battery_warn_v` · `battery_shutdown_v` · `tip_angle_deg` ·
> `obstacle_stop_cm` · `cmd_timeout_ms` · `link_loss_failsafe_ms` 의 **경계값이 정확히 등장해야** 하며,
> 없으면 CI 가 실패한다. **config 를 바꾸면 픽스처도 함께 바꾸도록 강제**하는 장치다.
> 자세한 내용은 [엔지니어링 가이드](ENGINEERING_GUIDE.md) 1장.

---

## 6. 검증 — 사람의 규칙 준수에 의존하지 않는다

정본은 문서가 아니라 **픽스처**다. 문서와 구현이 어긋나면 CI 가 잡는다.

| 파일 | 역할 |
| :--- | :--- |
| `tests/fixtures/protocol_samples.jsonl` | 유효 메시지 정본 (7종 + 경계값) |
| `tests/fixtures/protocol_invalid.jsonl` | 폐기·클램핑 대상 + 기대 동작 |
| `tests/test_protocol_fixtures.py` | 픽스처 자체의 일관성 검증 |

**송신측(Python)과 수신측(C++)은 같은 픽스처로 검증한다.** 한쪽만 바꾸면 CI 가 실패한다.

### 구현자 체크리스트

- [ ] 7종 전부 파싱되는가 (`protocol_samples.jsonl` 전 라인)
- [ ] 규칙 ①~④ 가 순서대로 동작하는가 (`protocol_invalid.jsonl` 의 `_expect` 대로)
- [ ] `step` 500 → 100 으로 **클램핑**되는가 (폐기가 아니라)
- [ ] `type: "FUTURE_CMD"` 수신 시 크래시 없이 WARN 만 남기는가
- [ ] 파싱 실패 패킷이 타임아웃 카운터를 갱신하지 **않는가**

> 새 타입을 추가하면 픽스처에도 한 줄 추가한다. 빠뜨리면
> `test_samples_cover_every_known_type` 이 잡는다.
