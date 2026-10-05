"""수평 LiDAR 입력 관문. MCU 시각 대신 같은 호스트 수신 시간축을 쓴다."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from host.common.lidar_link import Scan


@dataclass
class ScanGate:
    max_imu_age_ms: int = 250
    max_tilt_deg: float = 6.0
    # 보행 중 몸체 흔들림은 수평 단면 이탈이 작다 — 실기 lap_1: 보행 p50 6.3°·p90 10.7°, 정지 p90 1.3°, SCAN p50 14.4°.
    walking_tilt_deg: float = 12.0
    walking_window_ms: int = 700
    settle_ms: int = 750
    imu_pitch_offset_deg: float = 0.0
    imu_roll_offset_deg: float = 0.0
    pose_roll_offset_deg: float = 0.0
    _imu: deque[tuple[int, float, float]] = field(default_factory=lambda: deque(maxlen=256))
    # (송신 시각, 그 명령으로 시작한 수평 금지 구간의 끝). 기울임은 중립 복귀까지 유지.
    _pose_windows: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=256))
    _pose_tilted: bool = False
    _last_move_ms: int | None = None
    status: dict[str, Any] = field(default_factory=dict)

    def observe_imu(self, reading: Any, now_ms: int) -> None:
        pitch, roll = getattr(reading, "pitch", None), getattr(reading, "roll", None)
        if all(
            isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)
            for v in (pitch, roll)
        ):
            self._imu.append((now_ms, float(pitch), float(roll)))

    def note_pose(self, message: dict[str, Any], sent_ms: int) -> None:
        pitch = float(message.get("pitch", 0))
        roll = float(message.get("roll", 0)) - self.pose_roll_offset_deg
        tilted = max(abs(pitch), abs(roll)) > self.max_tilt_deg
        until = sent_ms + int(message.get("dur", 0)) + self.settle_ms
        # 직전의 지속 기울임은 중립 명령이 완료될 때까지 금지 구간으로 남긴다.
        if self._pose_tilted and self._pose_windows:
            start, _ = self._pose_windows[-1]
            self._pose_windows[-1] = (start, until)
        self._pose_windows.append((sent_ms, math.inf if tilted else until))
        self._pose_tilted = tilted

    def note_move(self, sent_ms: int) -> None:
        self._last_move_ms = sent_ms

    def check(self, scan: Scan, now_ms: int) -> str | None:
        end = scan.received_ms if scan.received_ms is not None else now_ms
        start = scan.started_ms if scan.started_ms is not None else end
        past = [v for v in self._imu if v[0] <= end]
        latest = past[-1] if past else None
        samples = [v for v in past if v[0] >= start - self.max_imu_age_ms]
        fresh = latest is not None and 0 <= end - latest[0] <= self.max_imu_age_ms
        walking = (
            self._last_move_ms is not None
            and 0 <= end - self._last_move_ms <= self.walking_window_ms
        )
        limit = self.walking_tilt_deg if walking else self.max_tilt_deg
        reason = None
        if self._pose_windows and end < self._pose_windows[-1][0] <= now_ms:
            # POSE 이전에 큐에 들어간 수평 스캔도 복귀 확인/재출발에는 쓰지 않는다.
            reason = "before_pose"
        elif any(start <= until and end >= sent for sent, until in self._pose_windows):
            reason = "pose_tilt" if self._pose_tilted else "pose_settling"
        elif fresh and any(
            max(abs(pitch - self.imu_pitch_offset_deg), abs(roll - self.imu_roll_offset_deg))
            > limit
            for _, pitch, roll in samples
        ):
            reason = "tilted"
        self.status = {
            "scan_rejected": reason,
            "imu_fresh": fresh,
            "tilt_limit_deg": limit,
            "imu_age_ms": None if latest is None else end - latest[0],
            "scan_received_ms": end,
            "scan_started_ms": start,
            "clear_allowed": reason is None,
        }
        return reason
