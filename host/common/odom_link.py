"""오도메트리 전달 링크 — Host PC → ROS2 컨테이너 (UDP · WBS 5.4.3).

**정본은 [docs/PROTOCOL_LIDAR.md](../../docs/PROTOCOL_LIDAR.md) 8절이다.**

호스트의 `host/slam/odometry.py` 가 만든 `odom` 자세를 컨테이너의
`docker/ros2/odom_bridge.py` 로 보내 `odom → base_link` tf 로 바꾼다. 로봇·중계
노드와 무관한 **호스트 안의 링크**라 기존 규약을 바꾸지 않는 추가다.

규칙은 **SCAN 링크(`lidar_link.py`)의 ①~⑤ 를 그대로 쓴다.** 같은 UDP 이고, 순찰기를
다시 켜면 `seq` 가 1 로 돌아오는 것도 같다 — 그래서 `boot_id` 는 **호스트 프로세스
한 번의 실행**이다. 점 배열이 없으므로 ⑥ 은 없다.

**이 모듈은 소켓을 만지지 않는다.** 실제 송신은 `tools/patrol_run.py`, 수신은
`docker/ros2/odom_bridge.py` 가 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 순서 게이트·판정은 `lidar_link.py` 와 같은 이유로 가져온다 — 규칙 ① 을 두 번 쓰지 않는다.
from host.common.protocol import (
    DecodeResult,
    Verdict,
    _is_int,
    _is_number,
    _known,
    _parse,
    _SeqGate,
    serialize,
)

ODOM_TYPES: frozenset[str] = frozenset({"ODOM"})

ODOM_REQUIRED: tuple[str, ...] = (
    "seq",
    "ts",
    "type",
    "device_id",
    "boot_id",
    "x_m",
    "y_m",
    "yaw_rad",
    "valid",
)

#: 좌표 필드. **단위를 이름에 싣는다** — 이 링크는 m·rad 로 보낸다. 로봇 규약의
#: mm·deg 와 다르므로 이름 없이 `x` 로 두면 어느 쪽인지 코드를 읽어야 안다.
#: `tools/lidar_live_map.py` 의 자세 입력(5202)이 이미 같은 이름을 쓴다.
POSE_FIELDS: tuple[str, ...] = ("x_m", "y_m", "yaw_rad")


@dataclass(frozen=True, slots=True)
class Odom:
    """받아들인 오도메트리 전문 하나. `valid` 가 거짓이면 좌표를 쓰지 않는다."""

    device_id: str
    boot_id: str
    seq: int
    ts_ms: int
    x_m: float
    y_m: float
    yaw_rad: float
    valid: bool


class OdomEncoder:
    """ODOM 전문을 만든다. `seq` 는 1 부터 단조 증가한다."""

    def __init__(self, device_id: str, boot_id: str) -> None:
        if not device_id or not boot_id:
            raise ValueError("device_id · boot_id 는 비어 있으면 안 됨")
        self._device_id = device_id
        self._boot_id = boot_id
        self._seq = 1

    def encode(self, *, ts_ms: int, x_m: float, y_m: float, yaw_rad: float, valid: bool) -> str:
        line = encode_odom(
            seq=self._seq,
            ts_ms=ts_ms,
            device_id=self._device_id,
            boot_id=self._boot_id,
            x_m=x_m,
            y_m=y_m,
            yaw_rad=yaw_rad,
            valid=valid,
        )
        self._seq += 1
        return line


def encode_odom(
    *,
    seq: int,
    ts_ms: int,
    device_id: str,
    boot_id: str,
    x_m: float,
    y_m: float,
    yaw_rad: float,
    valid: bool,
) -> str:
    return serialize(
        {
            "seq": seq,
            "ts": ts_ms,
            "type": "ODOM",
            "device_id": device_id,
            "boot_id": boot_id,
            "x_m": float(x_m),
            "y_m": float(y_m),
            "yaw_rad": float(yaw_rad),
            "valid": bool(valid),
        }
    )


class OdomDecoder:
    """ODOM 전문을 검증해 `Odom` 으로 바꾼다. 규칙 순서는 `ScanDecoder` 와 같다."""

    def __init__(self) -> None:
        self._gate = _SeqGate()

    def decode(self, raw: str | bytes) -> DecodeResult:
        parsed = _parse(raw)  # 규칙 ③
        if isinstance(parsed, DecodeResult):
            return parsed
        return self.validate(parsed)

    def validate(self, msg: dict[str, Any]) -> DecodeResult:
        # ── 규칙 ⑤ → ④ — 목록과 대조하기 전에 자료형을 본다 ──
        if not _known(msg.get("type"), ODOM_TYPES):
            if isinstance(msg.get("type"), str):
                return DecodeResult(Verdict.DISCARD_WARN, f"알 수 없는 타입: {msg['type']!r}")
            return DecodeResult(Verdict.DISCARD, "type 이 문자열이 아님")

        # ── 규칙 ② ──
        missing = [name for name in ODOM_REQUIRED if name not in msg]
        if missing:
            return DecodeResult(Verdict.DISCARD, f"필수 필드 누락: {missing}")

        # ── 규칙 ⑤ ──
        if not _is_int(msg["seq"]) or msg["seq"] < 1:
            return DecodeResult(Verdict.DISCARD, "seq 가 1 이상의 정수가 아님")
        if not _is_int(msg["ts"]) or msg["ts"] < 0:
            return DecodeResult(Verdict.DISCARD, "ts 가 0 이상의 정수 밀리초가 아님")
        for name in ("device_id", "boot_id"):
            if not isinstance(msg[name], str) or not msg[name]:
                return DecodeResult(Verdict.DISCARD, f"{name} 가 비어 있지 않은 문자열이 아님")
        for name in POSE_FIELDS:
            if not _is_number(msg[name]):
                return DecodeResult(Verdict.DISCARD, f"{name} 가 유한한 수가 아님")
        # `1`·`0` 을 참·거짓으로 받지 않는다 — 무효 자세가 유효로 읽히면 tf 가 나간다.
        if not isinstance(msg["valid"], bool):
            return DecodeResult(Verdict.DISCARD, "valid 가 bool 이 아님")

        # ── 규칙 ① — 내용 검증 뒤 (`ScanDecoder` 와 같은 이유) ──
        session = (msg["device_id"], msg["boot_id"])
        if not self._gate.admit(msg["seq"], session):
            last = self._gate.last_seq(session)
            return DecodeResult(Verdict.DISCARD, f"seq 역전·중복 (마지막 {last})")

        odom = Odom(
            device_id=msg["device_id"],
            boot_id=msg["boot_id"],
            seq=msg["seq"],
            ts_ms=msg["ts"],
            x_m=float(msg["x_m"]),
            y_m=float(msg["y_m"]),
            yaw_rad=float(msg["yaw_rad"]),
            valid=msg["valid"],
        )
        result_msg = dict(msg)
        result_msg["_odom"] = odom
        return DecodeResult(Verdict.ACCEPT, message=result_msg)


def odom_of(result: DecodeResult) -> Odom | None:
    """`DecodeResult` 에서 전문을 꺼낸다. 폐기된 결과에는 없다."""
    if not result.accepted or result.message is None:
        return None
    value = result.message.get("_odom")
    return value if isinstance(value, Odom) else None
