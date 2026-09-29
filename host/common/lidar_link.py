"""LiDAR 중계 노드 링크 — ESP32 DevKit → Host PC (UDP).

**정본은 이 파일이 아니라 [docs/PROTOCOL_LIDAR.md](../../docs/PROTOCOL_LIDAR.md) 다.**
`docs/PROTOCOL.md`가 그 문서를 LiDAR 링크의 정본 확장으로 지정한다. 새 링크는
**추가(additive)** 이므로 기존 제어·텔레메트리 규약은 바꾸지 않는다.

LiDAR 는 중계 MCU 가 UART 를 받아 UDP 로 올리는 세 번째 방향의 링크다 (ADR-6 ·
아키텍처 2절 LIDAR NODE). 규칙은 텔레메트리 링크의 것을 그대로 쓴다.

    ① 같은 `(device_id, boot_id)` 안에서 `seq` 역전·중복 폐기. 새 `boot_id` 의 `seq=1` 수락
    ② 필수 필드가 하나라도 없으면 폐기
    ③ 파싱 실패 시 폐기 · **타임아웃 카운터를 갱신하지 않는다**
    ④ 모르는 `type` 은 폐기 + WARN
    ⑤ 타입이 규약과 다르면 폐기 (목록과 대조하기 **전에** 문자열인지 확인한다)
    ⑥ 기형인 점은 그 점만 버리고 스캔은 살린다. 버린 수는 `Scan.dropped` 로 돌려준다

이 모듈은 소켓을 만지지 않는다 (ENGINEERING_GUIDE 2.1). 수신은 `tools/lidar_slam.py` ·
`tools/patrol_run.py` 가 한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# 규칙 ① 을 두 번 구현하지 않도록 비공개 `_SeqGate` 까지 그대로 가져온다.
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

#: 이 링크가 아는 타입. 지금은 하나뿐이고, 규칙 ④ 덕분에 추가는 하위 호환이다.
LIDAR_TYPES: frozenset[str] = frozenset({"SCAN"})

#: 필수 필드. `seq` 가 곧 Scan ID 다 (부팅 안에서 단조 증가).
SCAN_REQUIRED: tuple[str, ...] = ("seq", "ts", "type", "device_id", "boot_id", "points")

#: 전선 위의 점 `[angle_deg, dist_mm, quality?]` — LD19 UART 단위 그대로다 (PROTOCOL.md 2절).
POINT_MIN_LEN: int = 2
POINT_MAX_LEN: int = 3

#: 한 스캔의 점 개수 상한 — 기형 패킷 하나로 호스트 메모리를 밀어 올리지 못하게 한다.
MAX_POINTS: int = 2000


@dataclass(frozen=True, slots=True)
class Scan:
    """받아들인 스캔 하나(관측값). `points` 는 이미 내부 단위(rad · m)다 (`units.py`)."""

    device_id: str
    boot_id: str
    seq: int
    ts_ms: int
    #: `(angle_rad, dist_m)` 목록. 품질은 거를 근거가 없어 쓰지 않는다.
    points: tuple[tuple[float, float], ...]
    #: 규칙 ⑥ 으로 버린 점의 수. 계속 늘면 배선·전원을 의심한다.
    dropped: int = 0

    @property
    def scan_id(self) -> int:
        """`seq` 의 다른 이름. 시스템 문서의 Scan ID 가 이것이다."""
        return self.seq


def encode_scan(
    *,
    seq: int,
    ts_ms: int,
    device_id: str,
    boot_id: str,
    points_wire: list[list[float]],
) -> str:
    """스캔 한 줄을 만든다 — 중계 노드(C++)의 참조 구현. 목업과 픽스처 생성에서만 쓴다."""
    return serialize(
        {
            "seq": seq,
            "ts": ts_ms,
            "type": "SCAN",
            "device_id": device_id,
            "boot_id": boot_id,
            "points": points_wire,
        }
    )


def _valid_point(
    raw: Any, mount_yaw_rad: float = 0.0, angle_direction: int = 1
) -> tuple[float, float] | None:
    """점 하나를 검사해 내부 단위로 바꾼다. 기형이면 `None` (규칙 ⑥).

    ⚠️ 자료형과 길이를 먼저 본다 — `points: [3]` 같은 입력이 `TypeError` 로 수신 루프를 죽인다.
    """
    if not isinstance(raw, list | tuple) or not POINT_MIN_LEN <= len(raw) <= POINT_MAX_LEN:
        return None
    angle_deg, dist_mm = raw[0], raw[1]
    if not _is_number(angle_deg) or not _is_int(dist_mm):
        return None
    if dist_mm <= 0:
        # 0 은 LD19 계열이 "측정 실패"로 쓰는 값이고 음수는 물리적으로 없다.
        return None
    if len(raw) == POINT_MAX_LEN:
        quality = raw[2]
        if not _is_int(quality) or not 0 <= quality <= 255:
            return None
    # 설치각 보정은 여기 한 곳에서만 한다 — 두 번 돌리면 지도가 통째로 어긋난다.
    angle_rad = angle_direction * math.radians(float(angle_deg)) + mount_yaw_rad
    return angle_rad % (2 * math.pi), float(dist_mm) / 1000.0


def points_from_wire(
    points_wire: list[list[float]], mount_yaw_deg: float = 0.0, angle_direction: int = 1
) -> tuple[tuple[float, float], ...]:
    """전선 형식(`[angle_deg, dist_mm]`)을 디코더와 같은 규칙 ⑥ 으로 내부 단위로 바꾼다.

    `mount_yaw_deg` 기본값 0 은 로봇 기준 각도를 바로 만드는 시뮬레이션용이다.
    """
    if type(angle_direction) is not int or angle_direction not in (-1, 1):
        raise ValueError("angle_direction 은 -1 또는 1 이어야 함")
    mount_yaw_rad = math.radians(mount_yaw_deg)
    converted = (_valid_point(point, mount_yaw_rad, angle_direction) for point in points_wire)
    return tuple(point for point in converted if point is not None)


class ScanDecoder:
    """스캔 레코드를 검증해 `Scan` 으로 바꾼다. 순서는 `(device_id, boot_id)` 별로 센다."""

    def __init__(self, mount_yaw_deg: float = 0.0, angle_direction: int = 1) -> None:
        if type(angle_direction) is not int or angle_direction not in (-1, 1):
            raise ValueError("angle_direction 은 -1 또는 1 이어야 함")
        self._gate = _SeqGate()
        #: 라이다가 로봇에 돌아간 채로 붙은 각도. `lidar.mount_yaw_deg` 가 정본이다.
        self._mount_yaw_rad = math.radians(mount_yaw_deg)
        self._angle_direction = angle_direction

    def last_seq(self, device_id: str, boot_id: str) -> int | None:
        return self._gate.last_seq((device_id, boot_id))

    def forget_session(self, device_id: str, boot_id: str) -> None:
        self._gate.reset((device_id, boot_id))

    def decode(self, raw: str | bytes) -> DecodeResult:
        parsed = _parse(raw)  # 규칙 ③ — 파싱 실패는 링크를 갱신하지 않는다
        if isinstance(parsed, DecodeResult):
            return parsed
        return self.validate(parsed)

    def validate(self, msg: dict[str, Any]) -> DecodeResult:
        # ── 규칙 ⑤ — 목록과 대조하기 전에 자료형을 본다 ──
        if not _known(msg.get("type"), LIDAR_TYPES):
            if isinstance(msg.get("type"), str):
                # 문자열이지만 모르는 값 → ④ (WARN 은 기형과 섞지 않는다)
                return DecodeResult(Verdict.DISCARD_WARN, f"알 수 없는 타입: {msg['type']!r}")
            return DecodeResult(Verdict.DISCARD, "type 이 문자열이 아님")

        # ── 규칙 ② — 필수 필드 ──
        missing = [name for name in SCAN_REQUIRED if name not in msg]
        if missing:
            return DecodeResult(Verdict.DISCARD, f"필수 필드 누락: {missing}")

        # ── 규칙 ⑤ — 공통 필드의 자료형 ──
        if not _is_int(msg["seq"]) or msg["seq"] < 1:
            return DecodeResult(Verdict.DISCARD, "seq 가 1 이상의 정수가 아님")
        if not _is_int(msg["ts"]) or msg["ts"] < 0:
            return DecodeResult(Verdict.DISCARD, "ts 가 0 이상의 정수 밀리초가 아님")
        for name in ("device_id", "boot_id"):
            if not isinstance(msg[name], str) or not msg[name]:
                return DecodeResult(Verdict.DISCARD, f"{name} 가 비어 있지 않은 문자열이 아님")
        if not isinstance(msg["points"], list):
            return DecodeResult(Verdict.DISCARD, "points 가 배열이 아님")
        if len(msg["points"]) > MAX_POINTS:
            return DecodeResult(Verdict.DISCARD, f"points 가 {MAX_POINTS} 개를 초과함")

        # ── 규칙 ① — 순서 게이트 ──
        # 자료형 검증 뒤에 둔다 — 틀린 `seq` 가 게이트를 오염시키지 않게.
        session = (msg["device_id"], msg["boot_id"])
        if not self._gate.admit(msg["seq"], session):
            last = self._gate.last_seq(session)
            return DecodeResult(Verdict.DISCARD, f"seq 역전·중복 (마지막 {last})")

        # ── 규칙 ⑥ — 기형 점은 그 점만 버린다 ──
        points: list[tuple[float, float]] = []
        dropped = 0
        for raw_point in msg["points"]:
            point = _valid_point(raw_point, self._mount_yaw_rad, self._angle_direction)
            if point is None:
                dropped += 1
            else:
                points.append(point)

        scan = Scan(
            device_id=msg["device_id"],
            boot_id=msg["boot_id"],
            seq=msg["seq"],
            ts_ms=msg["ts"],
            points=tuple(points),
            dropped=dropped,
        )
        # 빈 스캔도 받아들인다 — 센서가 아무것도 못 본 것이지 링크 문제가 아니다.
        result_msg = dict(msg)
        result_msg["_scan"] = scan
        return DecodeResult(Verdict.ACCEPT, message=result_msg)


def scan_of(result: DecodeResult) -> Scan | None:
    """`DecodeResult` 에서 스캔을 꺼낸다. 폐기된 결과에는 없다."""
    if not result.accepted or result.message is None:
        return None
    value = result.message.get("_scan")
    return value if isinstance(value, Scan) else None
