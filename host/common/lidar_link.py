"""LiDAR 중계 노드 링크 — ESP32 DevKit → Host PC (UDP).

**정본은 이 파일이 아니라 [docs/PROTOCOL_LIDAR.md](../../docs/PROTOCOL_LIDAR.md) 다.**
`docs/PROTOCOL.md`가 그 문서를 LiDAR 링크의 정본 확장으로 지정한다. 새 링크는
**추가(additive)** 이므로 기존 제어·텔레메트리 규약은 바꾸지 않는다.

왜 별도 링크인가 — LiDAR 를 로봇 메인보드에 직결하지 않기로 했고(ADR-6), 중계
MCU 가 UART 를 받아 UDP 로 올린다(아키텍처 2절 LIDAR NODE). 즉 **제어 명령도
텔레메트리도 아닌 세 번째 방향**이라 기존 두 스키마에 얹을 자리가 없다.

규약을 새로 쓰지 않고 **텔레메트리 링크의 규칙을 그대로 가져왔다.** 같은 위험을
갖기 때문이다 — UDP 이고, 송신측이 MCU 이고, 수신측이 호스트다.

    ① 같은 `(device_id, boot_id)` 안에서 `seq` 역전·중복 폐기. 새 `boot_id` 의 `seq=1` 수락
    ② 필수 필드가 하나라도 없으면 폐기
    ③ 파싱 실패 시 폐기 · **타임아웃 카운터를 갱신하지 않는다**
    ④ 모르는 `type` 은 폐기 + WARN
    ⑤ 타입이 규약과 다르면 폐기 (목록과 대조하기 **전에** 문자열인지 확인한다)

`⑥` 하나만 새로 둔다 — **점 하나가 기형이면 그 점만 버리고 스캔은 살린다.**
360점 중 한 점이 깨졌다고 레코드를 통째로 폐기하면 그 사이클의 지도 갱신과
측위가 전부 사라진다. 명령의 규칙 ②(클램핑)가 "명령이 조용히 사라지는 것보다
낫다"고 판단한 것과 같은 종류의 선택이다. 버린 점의 수는 돌려주므로, 그것이
계속 늘면 호출자가 배선·전원을 의심할 수 있다 (아키텍처 2절 5V 핀 각주).

**이 모듈은 소켓을 만지지 않는다.** 바이트를 받아 판정과 스캔을 돌려줄 뿐이다
(ENGINEERING_GUIDE 2.1). 실제 수신은 `tools/lidar_slam.py` · `tools/patrol_run.py` 가 한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# 순서 게이트와 판정 결과는 **규약 구현을 둘로 만들지 않기 위해** 그대로 가져온다.
# `_SeqGate` 가 비공개인데도 재구현하지 않는 이유가 그것이다 — 같은 규칙 ① 을
# 두 번 쓰면 언젠가 한쪽만 고쳐진다.
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

#: 필수 필드. `seq` 가 곧 **Scan ID** 다 — 부팅 안에서 단조 증가하므로 별도
#: `scan_id` 를 두면 같은 것을 두 번 싣게 되고, 둘이 어긋날 때 무엇이 옳은지
#: 정할 근거가 없다.
SCAN_REQUIRED: tuple[str, ...] = ("seq", "ts", "type", "device_id", "boot_id", "points")

#: 전선 위의 단위 — **규약 본문과 같은 단위다** (PROTOCOL.md 2절).
#: 각도는 deg(실수), 거리는 mm(정수), 품질은 0~255(선택).
#: LD19 계열이 UART 로 내보내는 단위와도 같아서 중계 MCU 가 변환하지 않는다.
POINT_MIN_LEN: int = 2
POINT_MAX_LEN: int = 3

#: 한 스캔의 점 개수 상한. UDP 데이터그램 하나에 담기는 양을 넘으면 중계
#: 노드가 잘못 만든 것이다. 상한이 없으면 기형 패킷 하나로 호스트 메모리를
#: 밀어 올릴 수 있다.
MAX_POINTS: int = 2000


@dataclass(frozen=True, slots=True)
class Scan:
    """받아들인 스캔 하나. **판정 결과가 아니라 관측값이다.**

    `points` 는 이미 **내부 단위(rad · m)** 로 바뀌어 있다. 경계에서 한 번만
    변환하고 그 뒤로는 아무도 mm 를 보지 않는다 (`units.py` 머리말).
    """

    device_id: str
    boot_id: str
    seq: int
    ts_ms: int
    #: `(angle_rad, dist_m)` 목록. 품질은 여기서 쓰지 않으므로 버린다 —
    #: 임계를 정할 실측 근거가 없어서, 지금 거르면 근거 없는 규약이 된다.
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
    """스캔 한 줄을 만든다. **중계 노드(C++)의 참조 구현이다.**

    호스트가 이것을 보낼 일은 없고, 목업(`tools/mock_lidar.py`)과 픽스처
    생성에서만 쓴다. 그래도 파이썬 쪽에 두는 이유는 `protocol.py` 가
    `CommandEncoder` 를 두는 이유와 같다 — 펌웨어가 픽스처와 맞는지 볼 기준이
    필요하다.
    """
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


def _valid_point(raw: Any) -> tuple[float, float] | None:
    """점 하나를 검사해 내부 단위로 바꾼다. 기형이면 `None` (규칙 ⑥).

    ⚠️ **길이를 먼저 본다.** `raw[0]` 을 먼저 만지면 `points: [3]` 같은 입력에서
    `TypeError` 로 수신 루프가 죽는다 — 규칙 ⑤ 가 목록 대조 전에 문자열인지
    확인하는 것과 같은 이유다. UDP 로는 무엇이든 들어온다.
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
    return math.radians(float(angle_deg)) % (2 * math.pi), float(dist_mm) / 1000.0


def points_from_wire(points_wire: list[list[float]]) -> tuple[tuple[float, float], ...]:
    """전선 형식(`[angle_deg, dist_mm]`)을 내부 단위로 바꾼다.

    목업이 만든 점을 디코더를 거치지 않고 쓸 때를 위한 것이며, **규칙 ⑥ 과
    같은 함수를 쓴다** — 목업만 통과하는 다른 경로를 만들면 실기에서 처음
    검증을 지나게 된다.
    """
    converted = (_valid_point(point) for point in points_wire)
    return tuple(point for point in converted if point is not None)


class ScanDecoder:
    """스캔 레코드를 검증해 `Scan` 으로 바꾼다. 규칙 ①~⑥ 을 **이 순서로** 적용한다.

    `TelemetryDecoder` 와 같은 모양이며 같은 이유로 `(device_id, boot_id)` 별로
    순서를 센다 — 중계 노드를 2대 쓸 수 있고(아키텍처 2절 LIDAR NODE 수량 2),
    재부팅하면 `seq` 가 1 로 돌아온다.
    """

    def __init__(self) -> None:
        self._gate = _SeqGate()

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
                # 문자열이지만 모르는 값 → ④. WARN 은 "상대가 새 타입을 쓰기
                # 시작했다"는 신호 채널이므로 기형과 섞지 않는다.
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
        # ⚠️ **내용 검증 뒤, 점 변환 앞이다.** 순서는 데이터그램에 대한 것이고
        # 내용의 옳고 그름과 무관하지만(protocol.py 머리말 3번), 자료형이
        # 틀린 `seq` 를 게이트에 넣으면 게이트 자체가 오염된다.
        session = (msg["device_id"], msg["boot_id"])
        if not self._gate.admit(msg["seq"], session):
            last = self._gate.last_seq(session)
            return DecodeResult(Verdict.DISCARD, f"seq 역전·중복 (마지막 {last})")

        # ── 규칙 ⑥ — 기형 점은 그 점만 버린다 ──
        points: list[tuple[float, float]] = []
        dropped = 0
        for raw_point in msg["points"]:
            point = _valid_point(raw_point)
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
        # 빈 스캔도 **받아들인다.** 점이 하나도 없는 것은 링크 문제가 아니라
        # 센서가 아무것도 못 본 것이고(넓은 공터 · 차폐), 그 판단은 SLAM 이
        # 한다. 여기서 폐기하면 링크 타임아웃이 걸려 원인이 뒤바뀐다.
        result_msg = dict(msg)
        result_msg["_scan"] = scan
        return DecodeResult(Verdict.ACCEPT, message=result_msg)


def scan_of(result: DecodeResult) -> Scan | None:
    """`DecodeResult` 에서 스캔을 꺼낸다. 폐기된 결과에는 없다."""
    if not result.accepted or result.message is None:
        return None
    value = result.message.get("_scan")
    return value if isinstance(value, Scan) else None
