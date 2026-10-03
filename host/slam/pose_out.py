"""측위 포즈를 대시보드 쪽으로 보낸다 — 순찰 좌표를 표시(plan) 좌표로 변환한다.

지도 폴더의 `pose_frame.json` 이 순찰 좌표계 → 표시 좌표계 강체 변환을 담는다:

    x_d = cos(r)·x_p − sin(r)·y_p + t_x
    y_d = sin(r)·x_p + cos(r)·y_p + t_y
    yaw_d = wrap_pi(yaw_p + r)

`pose_frame.json` 이 없으면 송신하지 않는다 — 변환 없는 원시 순찰 좌표는 평면
좌표를 기다리는 뷰어에서 엉뚱한 자리에 찍힌다. «위치를 모른다» 보다 «틀린
위치를 아는 척» 하는 쪽이 나쁘다.

송신 형식은 대시보드 수신부(`PoseListener` → `telemetry.pose` → `setLivePose`)가
읽는 그대로다:

    {"x_m", "y_m", "yaw_rad", "lost", "moving", "ts_ms"}

`ts_ms` 는 `time.time` 기준이다 — 수신 측(`PoseReport`)도 그것을 기대한다.
"""

from __future__ import annotations

import contextlib
import json
import math
import socket
import time
from pathlib import Path
from typing import Any

from host.common.logging_setup import event_logger
from host.common.units import wrap_pi

LOG = event_logger("mechadog.pose_out")

POSE_FRAME_FILE = "pose_frame.json"


class PoseOut:
    """순찰 포즈를 표시 좌표로 변환해 UDP JSON 으로 흘려보낸다.

    송신 실패는 호출자에게 새지 않는다 — 대시보드가 꺼져 있어도 순찰은 돌아야 한다.
    """

    def __init__(
        self,
        transform: tuple[float, float, float],
        peer: tuple[str, int],
    ) -> None:
        """`transform` = `(회전 rad, t_x, t_y)`, `peer` = 대시보드 포즈 수신 주소."""
        rotate, t_x, t_y = transform
        self._cos, self._sin = math.cos(rotate), math.sin(rotate)
        self._rotate, self._tx, self._ty = rotate, t_x, t_y
        self._peer = peer
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    @classmethod
    def of(
        cls,
        maps_dir: Path,
        *,
        host: str,
        port: int,
    ) -> PoseOut | None:
        """지도 폴더의 `pose_frame.json` 으로 만든다. 파일이 없거나 포트가 잘못되면
        `None` — 송신 없음은 기능 저하이지 오류가 아니다."""
        path = maps_dir / POSE_FRAME_FILE
        if not path.is_file():
            return None
        if not 1 <= port <= 65535:
            raise ValueError(f"pose_out_port 는 1~65535 여야 함: {port}")
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        transform = (
            math.radians(float(raw["rotate_deg"])),
            float(raw["translate_x"]),
            float(raw["translate_y"]),
        )
        return cls(transform, (host, port))

    def send(
        self,
        pose: tuple[float, float, float],
        *,
        moving: bool,
        lost: bool = False,
        score_frac: float | None = None,
        zone: str | None = None,
        verified: bool | None = None,
    ) -> None:
        """순찰 좌표 `(x m, y m, yaw rad)` 를 표시 좌표로 변환해 보낸다."""
        x_d = self._cos * pose[0] - self._sin * pose[1] + self._tx
        y_d = self._sin * pose[0] + self._cos * pose[1] + self._ty
        packet = {
            "x_m": round(x_d, 4),
            "y_m": round(y_d, 4),
            "yaw_rad": round(wrap_pi(pose[2] + self._rotate), 4),
            "lost": lost,
            "moving": moving,
            "ts_ms": int(time.time() * 1000),
        }
        if score_frac is not None:
            packet["score_frac"] = round(score_frac, 3)
        if zone is not None:
            # 로봇 자신의 구역 판단 — 대시보드가 같은 라벨 지도로 내는 값과 같아야 한다.
            packet["robot_zone"] = zone
        if verified is not None:
            packet["verified"] = verified
        # 수신자가 없어도 순찰을 늦추지 않는다 — 포즈 표시는 부가 기능이다.
        with contextlib.suppress(OSError):
            self._sock.sendto(json.dumps(packet).encode("utf-8"), self._peer)

    def close(self) -> None:
        self._sock.close()
