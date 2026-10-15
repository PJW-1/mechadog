"""ROS2 지도 측위 반환 링크 — ROS2 컨테이너 → Host PC (UDP · WBS 5.4.4).

``slam_toolbox`` 이 만든 ``map -> base_link`` 자세를 Windows 순찰기로 돌려준다.
좌표는 항상 map 프레임의 m·rad 이며, 프레임 이름도 전문에 실어 잘못된 tf 를
조용히 받아들이지 않는다. 이 모듈은 소켓을 만지지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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

MAP_POSE_TYPES: frozenset[str] = frozenset({"MAP_POSE"})
MAP_FRAME = "map"
BASE_FRAME = "base_link"
MAP_POSE_REQUIRED = (
    "seq",
    "ts",
    "type",
    "device_id",
    "boot_id",
    "frame_id",
    "child_frame_id",
    "x_m",
    "y_m",
    "yaw_rad",
    "valid",
)


@dataclass(frozen=True, slots=True)
class MapPose:
    device_id: str
    boot_id: str
    seq: int
    ts_ms: int
    x_m: float
    y_m: float
    yaw_rad: float
    valid: bool


class MapPoseEncoder:
    def __init__(self, device_id: str, boot_id: str) -> None:
        if not device_id or not boot_id:
            raise ValueError("device_id · boot_id 는 비어 있으면 안 됨")
        self._device_id = device_id
        self._boot_id = boot_id
        self._seq = 1

    def encode(self, *, ts_ms: int, x_m: float, y_m: float, yaw_rad: float, valid: bool) -> str:
        line = encode_map_pose(
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


def encode_map_pose(
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
            "type": "MAP_POSE",
            "device_id": device_id,
            "boot_id": boot_id,
            "frame_id": MAP_FRAME,
            "child_frame_id": BASE_FRAME,
            "x_m": float(x_m),
            "y_m": float(y_m),
            "yaw_rad": float(yaw_rad),
            "valid": bool(valid),
        }
    )


class MapPoseDecoder:
    def __init__(self) -> None:
        self._gate = _SeqGate()

    def decode(self, raw: str | bytes) -> DecodeResult:
        parsed = _parse(raw)
        if isinstance(parsed, DecodeResult):
            return parsed
        return self.validate(parsed)

    def validate(self, msg: dict[str, Any]) -> DecodeResult:
        if not _known(msg.get("type"), MAP_POSE_TYPES):
            if isinstance(msg.get("type"), str):
                return DecodeResult(Verdict.DISCARD_WARN, f"알 수 없는 타입: {msg['type']!r}")
            return DecodeResult(Verdict.DISCARD, "type 이 문자열이 아님")
        missing = [name for name in MAP_POSE_REQUIRED if name not in msg]
        if missing:
            return DecodeResult(Verdict.DISCARD, f"필수 필드 누락: {missing}")
        if not _is_int(msg["seq"]) or msg["seq"] < 1:
            return DecodeResult(Verdict.DISCARD, "seq 가 1 이상의 정수가 아님")
        if not _is_int(msg["ts"]) or msg["ts"] < 0:
            return DecodeResult(Verdict.DISCARD, "ts 가 0 이상의 정수 밀리초가 아님")
        for name in ("device_id", "boot_id"):
            if not isinstance(msg[name], str) or not msg[name]:
                return DecodeResult(Verdict.DISCARD, f"{name} 가 비어 있지 않은 문자열이 아님")
        if msg["frame_id"] != MAP_FRAME or msg["child_frame_id"] != BASE_FRAME:
            return DecodeResult(Verdict.DISCARD, "좌표 프레임이 map -> base_link 가 아님")
        for name in ("x_m", "y_m", "yaw_rad"):
            if not _is_number(msg[name]):
                return DecodeResult(Verdict.DISCARD, f"{name} 가 유한한 수가 아님")
        if not isinstance(msg["valid"], bool):
            return DecodeResult(Verdict.DISCARD, "valid 가 bool 이 아님")
        session = (msg["device_id"], msg["boot_id"])
        if not self._gate.admit(msg["seq"], session):
            return DecodeResult(
                Verdict.DISCARD,
                f"seq 역전·중복 (마지막 {self._gate.last_seq(session)})",
            )
        pose = MapPose(
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
        result_msg["_map_pose"] = pose
        return DecodeResult(Verdict.ACCEPT, message=result_msg)


def map_pose_of(result: DecodeResult) -> MapPose | None:
    if not result.accepted or result.message is None:
        return None
    value = result.message.get("_map_pose")
    return value if isinstance(value, MapPose) else None
