"""TRACK 락온 제어기 — bbox 중심 x편차를 선회 보행으로 바꾼다 (WBS 3.5.4 · FR-3.5).

`ALERT`·`TRACK` 에서 «얼마나 돌지» 를 정하고 `TARGET_OFF_CENTER`·`TARGET_CENTERED` 판정
근거를 준다. 조향은 호(arc)로 한다 — 전진이 있어야 돈다 (ADR-11). 정지선에 닿았거나 편차가
크면 `TrackController.track` 이 이 지시를 제자리 회전(20°/30°)으로 덮어쓴다 (ADR-40).

    화면                     보내는 것
    ├─────┼──╳──┤            타겟이 오른쪽  → angle < 0 (우선회) · step 전진
    ├──╳──┼─────┤            타겟이 왼쪽    → angle > 0 (좌선회) · step 전진
    ├───╳═╪═╳───┤            데드존 안       → 정지, TARGET_CENTERED

- 편차 부호와 명령 부호는 반대다 — `angle` 은 양수가 좌회전(PROTOCOL 부호 규약)이고 화면 x 는
  오른쪽으로 커진다.
- 데드존을 뺀 나머지를 비례 구간으로 쓴다 — 경계에서 조향각이 0 에서 시작한다.
- 기체별 `gait_calibration.straight_bias_deg` 는 걷는 동안(데드존 밖)에만, 드리프트를
  거스르는 방향의 조향에만 더한다. 값이 없는 기체는 보정하지 않는다 (ADR-40 · 측정:
  `docs/measurements/2026-09-18-turn-rate-curve.md`).

## 거리 유지 (FR-3.5.2 · `3.5.8`)

bbox 높이로 한다 — 미터로 바꾸지 않는다 (ADR-15). 목표 높이(`track_target_height_px`)에
가까울수록 보폭을 줄이고 `track_stop_ratio` 를 넘으면 0 으로 세운다. 그 사이에서는
`track_min_step_mm` 아래로 내리지 않는다 — 전진이 없으면 조향도 죽는다 (ADR-40). 목표 높이가
없으면 거리 제어를 하지 않는다(추정값을 넣지 않는다).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from host.common.protocol import CLAMP_RANGES

__all__ = ["TrackCommand", "LockOnTracker"]


@dataclass(frozen=True, slots=True)
class TrackCommand:
    """한 프레임의 추종 지시.

    `centered` 는 조향 데드존 판정이다. FSM 정지는 거리(`step=0`)가 가른다.
    """

    step: float
    """전진 보폭 (mm). 정지선에 닿으면 0 이다."""

    angle: float
    """arc 조향각 (deg). **양수가 좌회전** (PROTOCOL 부호 규약)."""

    centered: bool
    """데드존 안인가."""

    deviation_px: float
    """화면 중앙 대비 x 편차 (px). 오른쪽이 양수. 로그·대시보드 표시용."""


class LockOnTracker:
    """bbox 중심 x편차를 `MOVE` 의 `step`·`angle` 로 바꾼다.

    상태가 없다 — 같은 입력이면 같은 출력이다. 추종 상실은 FR-3.7 타이머 소관이다.
    """

    def __init__(self, config: Mapping[str, Any]) -> None:
        fsm = config["fsm"]
        gait = config["gait"]
        deadzone = float(fsm["track_deadzone_px"])
        step = abs(float(gait["step_length_mm"]))
        turn = abs(float(gait["turn_angle_deg"]))
        if deadzone < 0:
            raise ValueError("track_deadzone_px 는 0 이상이어야 함")
        if step <= 0:
            raise ValueError("step_length_mm 이 0 이면 선회할 수 없다 (제자리 회전 미전제)")
        if turn <= 0:
            raise ValueError("turn_angle_deg 는 0 보다 커야 함")
        self._deadzone_px = deadzone
        self._step_mm = step
        self._max_turn_deg = turn
        # 보정값이 없는 기체는 보정하지 않는다 — 다른 기체의 값을 빌리지 않는다 (HARDWARE 3절).
        bias = (config.get("gait_calibration") or {}).get("straight_bias_deg")
        self._bias_deg = 0.0 if bias is None else float(bias)
        target_h = fsm.get("track_target_height_px")
        self._target_h_px = None if target_h is None else float(target_h)
        if self._target_h_px is not None and self._target_h_px <= 0:
            raise ValueError("track_target_height_px 는 0 보다 커야 함")
        self._stop_ratio = float(fsm["track_stop_ratio"])
        if self._stop_ratio <= 1.0:
            raise ValueError("track_stop_ratio 는 1.0 보다 커야 함")
        self._min_step_mm = float(fsm["track_min_step_mm"])
        if not 0.0 < self._min_step_mm <= self._step_mm:
            raise ValueError("track_min_step_mm 은 0 초과 step_length_mm 이하여야 함")

    @property
    def deadzone_px(self) -> float:
        return self._deadzone_px

    def _step_for(self, box_height: float | None) -> float:
        """목표 bbox 높이에 가까울수록 보폭을 줄인다 (FR-3.5.2 · `3.5.8`).

        최소 보폭에서 멈추며, 0 은 `track_stop_ratio` 를 넘었을 때뿐이다.
        """
        if self._target_h_px is None or box_height is None or box_height <= 0:
            return self._step_mm  # 목표 미측정 — 거리 제어를 하지 않는다
        ratio = float(box_height) / self._target_h_px
        if ratio >= self._stop_ratio:
            return 0.0
        if ratio <= 1.0:
            return self._step_mm  # 아직 멀다 — 최대로 간다
        # 목표(1.0)에서 정지선 사이를 최대 → 최소로 편다.
        t = (ratio - 1.0) / (self._stop_ratio - 1.0)
        return self._step_mm + t * (self._min_step_mm - self._step_mm)

    def update(
        self, center_x: float, frame_width: int, box_height: float | None = None
    ) -> TrackCommand:
        """한 프레임의 검출로 지시를 만든다.

        `center_x`·`frame_width`·`box_height` 는 모두 픽셀이다 (ADR-15). `box_height` 가
        없으면 거리 제어만 빠지고 조향은 그대로다.
        """
        if frame_width <= 0:
            raise ValueError("frame_width 는 1 이상이어야 함")

        midpoint = frame_width / 2.0
        deviation = float(center_x) - midpoint

        # 데드존이 반폭 이상이면 추종이 성립하지 않는다 — 설정 오류로 드러낸다.
        if self._deadzone_px >= midpoint:
            raise ValueError("track_deadzone_px 가 화면 반폭 이상이라 추종할 수 없다")

        if abs(deviation) <= self._deadzone_px:
            return TrackCommand(
                step=(
                    self._step_for(box_height)
                    if self._target_h_px is not None and box_height is not None
                    else 0.0
                ),
                angle=0.0,
                centered=True,
                deviation_px=deviation,
            )

        # 데드존을 뺀 나머지를 0~1 로 편다. 경계에서 0 이므로 각이 튀지 않는다.
        span = midpoint - self._deadzone_px
        magnitude = min((abs(deviation) - self._deadzone_px) / span, 1.0)

        # 편차 부호와 조향 부호는 반대다.
        angle = -magnitude * self._max_turn_deg if deviation > 0 else magnitude * self._max_turn_deg
        # 직진 편향은 걷는 동안, 드리프트를 거스르는 방향(보정값과 부호가 같은 조향)에만
        # 더한다 — 같은 방향 명령에 더하면 부호가 뒤집혀 그쪽으로 못 돈다 (ADR-40).
        # 더한 뒤 규약 범위로 자른다.
        if angle * self._bias_deg > 0:
            angle += self._bias_deg
        low, high = CLAMP_RANGES["angle"]
        angle = min(max(angle, low), high)
        return TrackCommand(
            step=self._step_for(box_height),
            angle=angle,
            centered=False,
            deviation_px=deviation,
        )
