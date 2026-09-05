"""통신 규약 구현 — 직렬화 · 파싱 · 검증 (WBS 3.1.1 · FR-5.1/5.2).

**정본은 이 파일이 아니라 docs/PROTOCOL.md 다.**
이 파일은 그 문서의 Python 구현이고, C++ 펌웨어(`command_parser`)는 같은 문서의
C++ 구현이다. 둘이 어긋나면 같은 골든 픽스처를 보는 CI 가 잡는다.

    tests/fixtures/protocol_samples.jsonl   ─┬─▶ Python  (tests/test_protocol.py)
    tests/fixtures/protocol_invalid.jsonl   ─┘   C++     (호스트 컴파일 · M1)

설계 결정 세 가지 — 나머지는 전부 여기서 따라 나온다.

1. **판정과 실행을 분리한다** (ENGINEERING_GUIDE 2.1).
   디코더는 로그를 남기지도, 소켓을 만지지도 않는다. `DecodeResult` 를 돌려줄
   뿐이고 WARN 을 남길지는 호출자가 정한다. 그래야 하드웨어 없이 pytest 로 닫힌다.

2. **시각은 주입받는다.** `time.time()` 을 직접 부르는 곳은 `system_clock_ms()`
   하나뿐이며, 인코더는 이것을 인자로 받는다. 테스트는 가짜 시계를 넣는다.

3. **seq 는 유효성과 무관하게 진행한다.** 내용 검증에서 폐기된 패킷이라도 그
   seq 는 "이미 지나간 순번"이다. 순서 게이트는 데이터그램의 순서에 대한 것이지
   내용의 옳고 그름에 대한 것이 아니다. 이렇게 해야 재전송된 옛 패킷이 뒤늦게
   받아들여지는 구멍이 생기지 않는다.

여기 있는 숫자들은 튜닝 파라미터가 아니라 **규약 상수**다 (DR-1, HW_MechDog API
허용 범위). 그래서 config.yaml 이 아니라 코드에 있다. 바꾸려면 파괴적 변경
절차를 따른다 (PROTOCOL.md 4절).
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# ══════════════════════════════════════════════════════════════
#  1. 규약 상수 — PROTOCOL.md 2절 · 5절
# ══════════════════════════════════════════════════════════════

#: 제어 명령 8종. 여기 없는 타입은 폐기 + WARN 이며, 그 덕분에 타입 추가는
#: 항상 하위 호환이다 (PROTOCOL.md 4절). `STATE` 가 그 첫 사례다.
COMMAND_TYPES: frozenset[str] = frozenset(
    {"MOVE", "POSE", "GAIT", "STOP", "ACTION", "LED", "SOUND", "STATE"}
)

#: 모든 명령의 공통 필수 필드. `seq`·`ts` 는 **정수**, `type` 은 문자열이다.
COMMON_REQUIRED: tuple[str, ...] = ("seq", "ts", "type")
COMMON_REQUIRED_SET: frozenset[str] = frozenset(COMMON_REQUIRED)

#: 타입별 필수 필드.
REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "MOVE": frozenset({"step", "angle"}),
    "POSE": frozenset({"pitch", "roll", "height", "dur"}),
    "GAIT": frozenset({"lift_time", "ground_time", "height"}),
    "STOP": frozenset(),
    "ACTION": frozenset({"id"}),
    "LED": frozenset({"color", "blink_hz"}),
    "SOUND": frozenset({"phrase_id"}),
    "STATE": frozenset({"state"}),
}

#: 문자열로 받는 필드. 나머지 필수 필드는 전부 수치다.
STRING_FIELDS: frozenset[str] = frozenset({"color", "state"})

#: 클램핑 대상 — PROTOCOL.md 가 범위를 명시한 필드만이다.
#: POSE·GAIT 는 "라이브러리 허용 범위"로만 규정되어 실측 전까지 클램핑하지
#: 않는다. 근거 없는 상한을 코드에 박으면 그것이 사실상의 규약이 되어버린다.
CLAMP_RANGES: dict[str, tuple[float, float]] = {
    "step": (-100, 100),  # mm
    "angle": (-30, 30),  # deg — arc 조향. 제자리 회전 불가 (DR-11)
    "id": (0, 15),  # 내장 액션 그룹
}

#: FSM 상태. 모르는 상태는 폐기 + WARN 이며, 명령 타입과 같은 이유로 상태
#: 추가를 하위 호환으로 만든다.
#:
#: **FSM 은 Host PC 에서 돈다.** 로봇은 자기가 `ALERT` 인지 `TRACK` 인지 알 수 없고
#: (그 판단의 근거인 카메라 영상을 호스트가 본다), 온보드가 스스로 아는 것은
#: `FAILSAFE`·`AVOID`·`PATROL` 셋뿐이다. 나머지는 `STATE` 명령으로 호스트가
#: 내려보내고 로봇은 받아적어 텔레메트리에 되돌려준다.
#:
#: 그래서 이 집합은 **명령(`STATE`)과 텔레메트리(`state`) 양쪽에서 쓰인다.**
#:
#: ⚠️ **정본은 아키텍처 문서의 전이표이며, 이 집합은 그것과 정확히 일치해야 한다.**
#: FR-4.2 가 대시보드에 "현재 FSM 상태"를 스트리밍하도록 요구하므로, 전이표에
#: 있고 여기 없는 상태는 **화면에 표시할 수 없는 상태**가 된다. 임의로 골라
#: 담으면 그 기준이 문서 어디에도 없어 반드시 다시 어긋난다 —
#: `test_fsm_states_match_the_transition_table` 이 대조한다.
FSM_STATES: frozenset[str] = frozenset(
    {
        # ── 온보드가 센서만으로 판정 가능 (Tier 1) ──
        "PATROL",
        "AVOID",
        "FAILSAFE",
        # ── 호스트 FSM 전용 — `STATE` 명령으로 내려온다 (Tier 2) ──
        "IDLE",
        "SCAN",
        "ALERT",
        "TRACK",
        "LOST",
        "AUTH_WAIT",
        "MANUAL",
        # ── Phase 2 — 측위가 확보된 뒤에 쓰인다 ──
        "HAZARD_DISPATCH",
        "HAZARD_SCAN",
        "ZONE_INSPECT",
    }
)

#: 온보드가 센서만으로 판정할 수 있는 상태. 나머지는 호스트가 알려줘야 한다.
ONBOARD_STATES: frozenset[str] = frozenset({"PATROL", "AVOID", "FAILSAFE"})

#: 텔레메트리 필수 필드 — PROTOCOL.md 5절 규칙 ②가 열거한 그대로다.
#: 여기 없는 필드(`ts`·`dist_cm`·`batt_v`)는 **있을 때만** 검증한다. 규약이
#: 요구하지 않는 필드를 구현이 요구하면, 규약을 지킨 송신측이 조용히 폐기된다.
TELEMETRY_REQUIRED: tuple[str, ...] = ("seq", "device_id", "state", "imu", "flags")
IMU_FIELDS: tuple[str, ...] = ("pitch", "roll", "yaw")
FLAG_FIELDS: tuple[str, ...] = ("lowbatt", "tipped", "link_ok")

#: `TelemetryEncoder` 가 스스로 채우며 `extra` 로 덮을 수 없는 필드.
#: 나머지 본문 필드(`state`·`dist_cm`·`imu`·`batt_v`·`last_cmd_age_ms`·`flags`)는
#: `build()` 의 **명명 인자**이므로 애초에 `extra` 에 닿지 않는다 — 파이썬이 막는다.
TELEMETRY_MANAGED: frozenset[str] = frozenset({"seq", "ts", "device_id"})

#: 2S 리튬 물리 범위 (셀당 3.0~4.2V). 이 밖의 값은 측정 오류이므로 폐기한다.
#: 저전압 **판정** 임계(config.safety.battery_*)와는 다른 것이다 — 이것은
#: "물리적으로 가능한가"이고 그것은 "안전한가"이다.
BATT_MIN_V: float = 6.0
BATT_MAX_V: float = 8.4

#: 메타 필드 — 픽스처의 주석용이며 전송 대상이 아니다.
META_FIELDS: frozenset[str] = frozenset({"_case", "_expect"})


# ══════════════════════════════════════════════════════════════
#  2. 판정 결과
# ══════════════════════════════════════════════════════════════


class Verdict(StrEnum):
    """수신 판정. 값은 골든 픽스처의 `_expect` 와 문자열까지 일치한다."""

    ACCEPT = "accept"
    CLAMP = "clamp"  # 받아들이되 범위를 잘랐다
    DISCARD = "discard"
    DISCARD_WARN = "discard_warn"  # 폐기 + WARN 로그 (규약 확장의 근거)


@dataclass(frozen=True, slots=True)
class DecodeResult:
    """파싱·검증 결과. 로깅과 부수효과는 호출자의 몫이다."""

    verdict: Verdict
    reason: str = ""
    message: dict[str, Any] | None = None
    clamped: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.verdict in (Verdict.ACCEPT, Verdict.CLAMP)

    @property
    def warns(self) -> bool:
        return self.verdict is Verdict.DISCARD_WARN

    @property
    def refreshes_link(self) -> bool:
        """규칙 ③ — 받아들인 패킷만 링크 타임아웃 카운터를 갱신한다.

        깨진 패킷을 "살아 있음"으로 세면 페일세이프가 걸리지 않는다.
        수신 루프는 반드시 이 값을 보고 카운터를 갱신해야 한다.
        """
        return self.accepted


def _accept(msg: dict[str, Any], clamped: tuple[str, ...]) -> DecodeResult:
    verdict = Verdict.CLAMP if clamped else Verdict.ACCEPT
    return DecodeResult(verdict, message=msg, clamped=clamped)


# ══════════════════════════════════════════════════════════════
#  3. 공통 도구
# ══════════════════════════════════════════════════════════════


def system_clock_ms() -> int:
    """Host PC 기준 epoch 밀리초 (초 아님).

    이 파일에서 실제 시각을 읽는 유일한 지점이다. 테스트는 인코더에 가짜
    시계를 주입하므로 이 함수를 거치지 않는다.
    """
    return int(time.time() * 1000)


def _is_number(value: Any) -> bool:
    """bool 은 수치로 보지 않는다 — `True` 가 `1` 로 통과하면 안 된다."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    """`seq`·`ts` 전용. 실수를 허용하면 조용히 잘려 나간다.

    `seq: 1.5` 를 받아들이면 순서 게이트는 `int()` 로 잘라 `1` 로 기억하고
    메시지에는 `1.5` 가 남아 **둘이 어긋난다.** 그리고 뒤이어 오는 정상적인
    `seq: 1` 이 "중복"으로 폐기된다. 규약이 정수라고 못박은 이유가 이것이다.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _known(value: Any, allowed: frozenset[str]) -> bool:
    """문자열인지 먼저 확인한 뒤 목록과 대조한다.

    ⚠️ **이 순서가 안전 장치다.** UDP 로는 무엇이든 들어온다. `{"type": []}` 를
    받으면 `[] in frozenset(...)` 이 `TypeError: unhashable type` 을 던지고
    수신 루프가 죽는다 — 관제가 멈추는 것이므로 폐기보다 나쁘다.
    """
    return isinstance(value, str) and value in allowed


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def apply_clamps(msg: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """범위를 벗어난 필드를 자른다 (규칙 ②).

    폐기하지 않는 이유는 PROTOCOL.md 3절에 있다 — 명령이 조용히 사라지는
    것보다 잘린 명령이 낫다. 원본을 바꾸지 않고 새 dict 를 돌려준다.
    """
    out = dict(msg)
    clamped: list[str] = []
    for name, (low, high) in CLAMP_RANGES.items():
        value = out.get(name)
        if not _is_number(value):
            continue
        limited = clamp(value, low, high)
        if limited != value:
            out[name] = limited
            clamped.append(name)
    return out, tuple(sorted(clamped))


def serialize(msg: dict[str, Any]) -> str:
    """한 줄 JSON 으로 만든다. 줄바꿈이 없어야 JSONL·UDP 양쪽에서 안전하다."""
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False)


def strip_meta(msg: dict[str, Any]) -> dict[str, Any]:
    """픽스처의 `_case`·`_expect` 를 걷어낸다. 전송 대상이 아니다."""
    return {k: v for k, v in msg.items() if k not in META_FIELDS}


def _parse(raw: str | bytes) -> dict[str, Any] | DecodeResult:
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        # 규칙 ③ — 폐기하되 타임아웃 카운터를 갱신하지 않는다.
        return DecodeResult(Verdict.DISCARD, "파싱 실패")
    if not isinstance(msg, dict):
        return DecodeResult(Verdict.DISCARD, "최상위가 JSON 객체가 아님")
    return msg


class _SeqGate:
    """seq 역전·중복 폐기 (규칙 ①).

    송신자별로 따로 센다. 3대를 동시에 운용하므로 한 카운터로 묶으면 개체끼리
    서로의 패킷을 폐기한다 (WBS 7절).
    """

    def __init__(self) -> None:
        self._last: dict[str, int] = {}

    def last_seq(self, sender: str = "") -> int | None:
        return self._last.get(sender)

    def admit(self, seq: int, sender: str = "") -> bool:
        last = self._last.get(sender)
        if last is not None and seq <= last:
            return False
        self._last[sender] = seq
        return True


# ══════════════════════════════════════════════════════════════
#  4. 제어 명령 — Host PC → MechDog
# ══════════════════════════════════════════════════════════════


class CommandEncoder:
    """제어 명령 직렬화. `seq` 는 단조 증가하고 `ts` 는 주입된 시계에서 온다.

    송신은 10Hz 고정이며 변화가 없어도 계속 보낸다. 수신측 타임아웃을 갱신하는
    것이 곧 하트비트이기 때문이다 (PROTOCOL.md 1절).
    """

    def __init__(self, clock: Callable[[], int] = system_clock_ms, start_seq: int = 1) -> None:
        self._clock = clock
        self._seq = start_seq

    @property
    def next_seq(self) -> int:
        return self._seq

    def build(self, type_: str, **fields: Any) -> dict[str, Any]:
        """명령 dict 를 만든다. 범위를 벗어난 값은 보내기 전에 자른다.

        모르는 타입·필수 필드 누락은 **송신측 버그**이므로 조용히 넘기지 않고
        예외를 낸다. 수신측의 관용(폐기·클램핑)과 대칭이 아닌 것은 의도적이다.
        """
        if type_ not in COMMAND_TYPES:
            raise ValueError(f"알 수 없는 명령 타입: {type_!r}")
        # 예약 키를 넘기면 자동 생성값이 덮여 **단조 증가 seq 보장이 깨진다.**
        # 인코더가 지키는 유일한 계약이므로 우회를 허용하지 않는다.
        reserved = COMMON_REQUIRED_SET & fields.keys()
        if reserved:
            raise ValueError(f"인코더가 관리하는 필드는 넘길 수 없다: {sorted(reserved)}")
        missing = REQUIRED_FIELDS[type_] - fields.keys()
        if missing:
            raise ValueError(f"{type_} 필수 필드 누락: {sorted(missing)}")
        if type_ == "STATE" and not _known(fields["state"], FSM_STATES):
            raise ValueError(f"알 수 없는 상태: {fields['state']!r}")

        msg = {"seq": self._seq, "ts": self._clock(), "type": type_, **fields}
        self._seq += 1
        msg, _ = apply_clamps(msg)
        return msg

    def encode(self, type_: str, **fields: Any) -> str:
        return serialize(self.build(type_, **fields))

    # ── 편의 메서드 ── 호출부가 필드명을 외우지 않아도 되게 한다
    def move(self, step: float, angle: float) -> str:
        return self.encode("MOVE", step=step, angle=angle)

    def pose(self, pitch: float, roll: float, height: float, dur: int) -> str:
        return self.encode("POSE", pitch=pitch, roll=roll, height=height, dur=dur)

    def gait(self, lift_time: int, ground_time: int, height: float) -> str:
        return self.encode("GAIT", lift_time=lift_time, ground_time=ground_time, height=height)

    def stop(self) -> str:
        return self.encode("STOP")

    def action(self, action_id: int) -> str:
        return self.encode("ACTION", id=action_id)

    def led(self, color: str, blink_hz: float) -> str:
        return self.encode("LED", color=color, blink_hz=blink_hz)

    def sound(self, phrase_id: int) -> str:
        return self.encode("SOUND", phrase_id=phrase_id)

    def state(self, state: str) -> str:
        """호스트의 FSM 상태를 로봇에게 알려준다.

        로봇은 이것을 판단 근거로 쓰지 않는다 — **받아적어 텔레메트리에 되돌려줄
        뿐이다.** Tier 1 안전 판정은 이 값과 무관하게 온보드가 우선한다
        (아키텍처 1.2 불변 규칙). 즉 호스트가 `PATROL` 이라고 해도 로봇이 전도를
        감지했다면 로봇은 `FAILSAFE` 를 보고한다.
        """
        return self.encode("STATE", state=state)


class CommandDecoder:
    """제어 명령 수신 검증. C++ 파서와 규칙·순서가 같아야 한다.

    이 클래스가 Python 쪽에 있는 이유는 가상 MechDog(WBS 6.1.1)이 실물 없이
    같은 규칙으로 수신해야 하기 때문이다. 펌웨어의 참조 구현이기도 하다.

    규칙 적용 순서 — PROTOCOL.md 3절의 ①~④ 를 실행 가능한 순서로 편 것이다.
    seq 를 읽으려면 먼저 파싱과 공통 필드 확인이 끝나야 하므로 앞에 온다.
    """

    def __init__(self) -> None:
        self._gate = _SeqGate()

    @property
    def last_seq(self) -> int | None:
        return self._gate.last_seq()

    def decode(self, raw: str | bytes) -> DecodeResult:
        parsed = _parse(raw)
        if isinstance(parsed, DecodeResult):
            return parsed
        return self.validate(parsed)

    def validate(self, msg: dict[str, Any]) -> DecodeResult:
        msg = strip_meta(msg)

        for key in COMMON_REQUIRED:
            if key not in msg:
                return DecodeResult(Verdict.DISCARD, f"공통 필수 필드 누락: {key}")
        for key in ("seq", "ts"):
            if not _is_int(msg[key]):
                return DecodeResult(Verdict.DISCARD, f"{key} 가 정수가 아님")
        if not isinstance(msg["type"], str):
            return DecodeResult(Verdict.DISCARD, "type 이 문자열이 아님")

        # ① seq 역전·중복
        if not self._gate.admit(msg["seq"]):
            return DecodeResult(Verdict.DISCARD, "seq 역전·중복")

        # ④ 모르는 타입 — 폐기 + WARN
        type_ = msg["type"]
        if type_ not in COMMAND_TYPES:
            return DecodeResult(Verdict.DISCARD_WARN, f"알 수 없는 타입: {type_!r}")

        for name in sorted(REQUIRED_FIELDS[type_]):
            if name not in msg:
                return DecodeResult(Verdict.DISCARD, f"{type_} 필수 필드 누락: {name}")
            value = msg[name]
            if name in STRING_FIELDS:
                if not isinstance(value, str):
                    return DecodeResult(Verdict.DISCARD, f"{name} 가 문자열이 아님")
            elif not _is_number(value):
                return DecodeResult(Verdict.DISCARD, f"{name} 가 수치가 아님")

        # 모르는 상태값 — 폐기 + WARN. 텔레메트리 규칙 ③과 대칭이며, 같은
        # 이유로 상태 추가를 하위 호환으로 만든다.
        if type_ == "STATE" and not _known(msg["state"], FSM_STATES):
            return DecodeResult(Verdict.DISCARD_WARN, f"알 수 없는 상태: {msg['state']!r}")

        # ② 범위 초과는 폐기가 아니라 클램핑
        return _accept(*apply_clamps(msg))


# ══════════════════════════════════════════════════════════════
#  5. 텔레메트리 — MechDog → Host PC
# ══════════════════════════════════════════════════════════════


class TelemetryEncoder:
    """텔레메트리 직렬화. 펌웨어(C++)의 대응물이며, 가상 MechDog 가 쓴다.

    ESP32 는 파일 로깅을 하지 않는다. **텔레메트리가 곧 로그다.**
    """

    def __init__(
        self,
        device_id: str,
        clock: Callable[[], int] = system_clock_ms,
        start_seq: int = 1,
    ) -> None:
        if not device_id:
            raise ValueError("device_id 는 필수다 — IP 로 송신자를 구분하면 안 된다 (DR-17)")
        self._device_id = device_id
        self._clock = clock
        self._seq = start_seq

    @property
    def next_seq(self) -> int:
        return self._seq

    def build(
        self,
        state: str,
        dist_cm: float,
        imu: dict[str, float],
        batt_v: float,
        last_cmd_age_ms: int,
        flags: dict[str, bool],
        **extra: Any,
    ) -> dict[str, Any]:
        if not _known(state, FSM_STATES):
            raise ValueError(f"알 수 없는 상태: {state!r}")
        missing_imu = [f for f in IMU_FIELDS if f not in imu]
        if missing_imu:
            raise ValueError(f"imu 필드 누락: {missing_imu}")
        missing_flags = [f for f in FLAG_FIELDS if f not in flags]
        if missing_flags:
            raise ValueError(f"flags 필드 누락: {missing_flags}")
        # `extra` 로 `device_id` 를 덮으면 **송신자를 위조할 수 있다.** 다중 개체
        # 운용에서 그것이 유일한 구분 수단이므로(DR-17) 우회를 허용하지 않는다.
        reserved = TELEMETRY_MANAGED & extra.keys()
        if reserved:
            raise ValueError(f"인코더가 관리하는 필드는 넘길 수 없다: {sorted(reserved)}")

        msg = {
            "seq": self._seq,
            "ts": self._clock(),
            "device_id": self._device_id,
            "state": state,
            "dist_cm": dist_cm,
            "imu": dict(imu),
            "batt_v": batt_v,
            "last_cmd_age_ms": last_cmd_age_ms,
            "flags": dict(flags),
            **extra,
        }
        self._seq += 1
        return msg

    def encode(self, *args: Any, **kwargs: Any) -> str:
        return serialize(self.build(*args, **kwargs))


class TelemetryDecoder:
    """텔레메트리 수신 검증 (PROTOCOL.md 5절).

    제어 명령과 대칭이지만 방향이 반대다 — 송신이 펌웨어(L1·L2), 수신이
    호스트(팀장)이므로 같은 인터페이스 위험을 갖는다. 그래서 규칙도 대칭으로 둔다.
    """

    def __init__(self) -> None:
        self._gate = _SeqGate()

    def last_seq(self, device_id: str) -> int | None:
        return self._gate.last_seq(device_id)

    def decode(self, raw: str | bytes) -> DecodeResult:
        parsed = _parse(raw)
        if isinstance(parsed, DecodeResult):
            return parsed
        return self.validate(parsed)

    def validate(self, msg: dict[str, Any]) -> DecodeResult:
        msg = strip_meta(msg)

        # ② 필수 필드
        for key in TELEMETRY_REQUIRED:
            if key not in msg:
                return DecodeResult(Verdict.DISCARD, f"필수 필드 누락: {key}")
        if not _is_int(msg["seq"]):
            return DecodeResult(Verdict.DISCARD, "seq 가 정수가 아님")
        # `ts` 는 규칙 ②의 필수 목록에 없으므로 있을 때만 본다.
        if "ts" in msg and not _is_int(msg["ts"]):
            return DecodeResult(Verdict.DISCARD, "ts 가 정수가 아님")
        if not isinstance(msg["device_id"], str) or not msg["device_id"]:
            return DecodeResult(Verdict.DISCARD, "device_id 가 비어 있음")

        imu, flags = msg["imu"], msg["flags"]
        if not isinstance(imu, dict) or not isinstance(flags, dict):
            return DecodeResult(Verdict.DISCARD, "imu·flags 가 객체가 아님")
        for name in IMU_FIELDS:
            if not _is_number(imu.get(name)):
                return DecodeResult(Verdict.DISCARD, f"imu.{name} 누락 또는 비수치")
        for name in FLAG_FIELDS:
            if not isinstance(flags.get(name), bool):
                return DecodeResult(Verdict.DISCARD, f"flags.{name} 누락 또는 비불리언")

        # ① seq 역전·중복 — 개체별로 센다
        if not self._gate.admit(msg["seq"], msg["device_id"]):
            return DecodeResult(Verdict.DISCARD, "seq 역전·중복")

        # ③ 모르는 상태 — 폐기 + WARN
        #
        # ⚠️ **기형과 미지를 구분한다.** WARN 은 "상대가 새 상태를 쓰기
        # 시작했다"는 신호 채널이므로(하위 호환 확장의 근거), 문자열이 아닌
        # 기형 값을 여기 섞으면 그 신호가 묻힌다. 기형은 조용히 폐기한다.
        state = msg["state"]
        if not isinstance(state, str):
            return DecodeResult(Verdict.DISCARD, "state 가 문자열이 아님")
        if not _known(state, FSM_STATES):
            return DecodeResult(Verdict.DISCARD_WARN, f"알 수 없는 상태: {state!r}")

        # ④ 물리적으로 불가능한 값
        batt_v = msg.get("batt_v")
        if batt_v is not None and (
            not _is_number(batt_v) or not BATT_MIN_V <= batt_v <= BATT_MAX_V
        ):
            return DecodeResult(Verdict.DISCARD, f"batt_v 물리 범위 이탈: {batt_v}")
        dist_cm = msg.get("dist_cm")
        if dist_cm is not None and (not _is_number(dist_cm) or dist_cm < 0):
            return DecodeResult(Verdict.DISCARD, f"dist_cm 음수: {dist_cm}")

        # ⑤ 플래그와 상태의 모순 — 안전측으로 폐기 + WARN
        # `tipped` 는 온보드 안전 로직이 이미 발동했다는 뜻이므로 상태는
        # FAILSAFE 여야 한다. 순찰 중이라고 보고하는 것은 둘 중 하나가 틀린
        # 것이고, 어느 쪽이든 그 레코드를 믿고 대시보드를 갱신하면 안 된다.
        # (`lowbatt` 는 경고 수준이라 PATROL 과 공존할 수 있다 — 모순이 아니다.)
        if flags["tipped"] and state != "FAILSAFE":
            return DecodeResult(Verdict.DISCARD_WARN, f"tipped 인데 state={state}")

        return _accept(msg, ())
