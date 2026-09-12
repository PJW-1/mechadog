"""TRACK 락온 제어기 — bbox 중심 x편차를 선회 보행으로 바꾼다 (WBS 3.5.4 · FR-3.5).

`ALERT` 에서 타겟이 화면 중앙을 벗어나면 `TRACK` 으로 가고, 이 파일이 **얼마나
돌지**를 정한다. FSM 은 *언제* 도는지만 안다 — `TARGET_OFF_CENTER` 와
`TARGET_CENTERED` 는 전이표에 있지만 그것을 **내는 쪽이 없었다.** 여기가 그 자리다.

**제자리 회전이 불가하다** (DR-11). 그래서 조향은 곧 걷기다 — `MOVE` 의 `angle`
만 주고 `step` 을 0 으로 두면 로봇은 아무 데도 향하지 못한다. 편차가 남아 있는
동안은 **걸으면서** 방향을 맞춘다.

    화면                     보내는 것
    ├─────┼──╳──┤            타겟이 오른쪽  → angle < 0 (우선회) · step 전진
    ├──╳──┼─────┤            타겟이 왼쪽    → angle > 0 (좌선회) · step 전진
    ├───╳═╪═╳───┤            데드존 안       → 정지, TARGET_CENTERED
         데드존

⚠️ **부호를 뒤집으면 타겟에서 멀어진다.** `PROTOCOL` 부호 규약은 *양수 = 반시계 =
로봇의 좌회전* 이다. 화면 좌표 x 는 오른쪽으로 커지므로, 타겟이 오른쪽에 있으면
편차가 **양수**이고 로봇은 **우회전**해야 한다 — 즉 `angle` 은 **음수**다. 편차
부호와 명령 부호가 반대라는 것이 이 파일에서 가장 틀리기 쉬운 곳이다.

⚠️ **데드존 경계에서 명령이 튀지 않게 한다.** 편차를 그대로 비례시키면 데드존을
벗어나는 순간 조향각이 0 에서 갑자기 솟는다. 그래서 **데드존을 뺀 나머지**를
비례 구간으로 쓴다 — 경계에서 0 으로 시작해 화면 끝에서 최대가 된다. 데드존은
*"흔들지 않는 구간"* 이지 *"넘으면 확 꺾는 문턱"* 이 아니다.

⚠️ **경계 히스테리시스는 넣지 않았다.** bbox 중심이 데드존 경계에 딱 걸치면
`TRACK` 과 `ALERT` 사이를 오갈 수 있다. 막으려면 진입·이탈 임계를 따로 둬야
하는데, **그 간격을 정하려면 실제 bbox 중심이 얼마나 떨리는지를 재야 한다.**
재지 않은 값을 넣으면 좁아서 무용하거나 넓어서 추종이 굼떠진다. 실측 후 `config`
에 임계를 추가하는 것이 순서다 (`gait_calibration` 이 없으면 회피 구간을 만들지
않는 `actions.avoid_phases` 와 같은 원칙).

거리 유지(FR-3.5.2)는 여기 없다. bbox **높이**로 하는 별개 제어이며 `3.5.4` 의
완료 기준이 아니다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["TrackCommand", "LockOnTracker"]


@dataclass(frozen=True, slots=True)
class TrackCommand:
    """한 프레임의 추종 지시.

    `centered` 가 FSM 사건을 가른다 — 참이면 `TARGET_CENTERED`(→ `ALERT`),
    거짓이면 `TARGET_OFF_CENTER`(→ `TRACK`).
    """

    step: float
    """전진 보폭 (mm). 중앙에 들어오면 0 이다."""

    angle: float
    """arc 조향각 (deg). **양수가 좌회전** (PROTOCOL 부호 규약)."""

    centered: bool
    """데드존 안인가."""

    deviation_px: float
    """화면 중앙 대비 x 편차 (px). 오른쪽이 양수. 로그·대시보드 표시용."""


class LockOnTracker:
    """bbox 중심 x편차를 `MOVE` 의 `step`·`angle` 로 바꾼다.

    상태를 들고 있지 않다 — 같은 입력이면 항상 같은 출력이다. 추종이 끊긴 판단은
    `FR-3.7` 의 5초 타이머 소관이라 여기서 시간을 재지 않는다.
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
            raise ValueError("step_length_mm 이 0 이면 선회할 수 없다 (제자리 회전 불가)")
        if turn <= 0:
            raise ValueError("turn_angle_deg 는 0 보다 커야 함")
        self._deadzone_px = deadzone
        self._step_mm = step
        self._max_turn_deg = turn

    @property
    def deadzone_px(self) -> float:
        return self._deadzone_px

    def update(self, center_x: float, frame_width: int) -> TrackCommand:
        """한 프레임의 검출 중심으로 지시를 만든다.

        `center_x` 는 bbox 중심의 화면 x 좌표, `frame_width` 는 프레임 폭이다.
        둘 다 픽셀이며 **미터로 바꾸지 않는다** (DR-15 · FR-3.5.2 와 같은 이유).
        """
        if frame_width <= 0:
            raise ValueError("frame_width 는 1 이상이어야 함")

        midpoint = frame_width / 2.0
        deviation = float(center_x) - midpoint

        # 데드존 밖으로 나갈 수 없는 화면이면(데드존이 반폭 이상) 추종이 성립하지
        # 않는다. 조용히 정지시키지 않고 설정 오류로 드러낸다.
        if self._deadzone_px >= midpoint:
            raise ValueError("track_deadzone_px 가 화면 반폭 이상이라 추종할 수 없다")

        if abs(deviation) <= self._deadzone_px:
            return TrackCommand(step=0.0, angle=0.0, centered=True, deviation_px=deviation)

        # 데드존을 뺀 나머지를 0~1 로 편다. 경계에서 0 이므로 각이 튀지 않는다.
        span = midpoint - self._deadzone_px
        magnitude = min((abs(deviation) - self._deadzone_px) / span, 1.0)

        # 편차 부호와 조향 부호는 **반대다** — 위 주석 참조.
        angle = -magnitude * self._max_turn_deg if deviation > 0 else magnitude * self._max_turn_deg
        return TrackCommand(
            step=self._step_mm,
            angle=angle,
            centered=False,
            deviation_px=deviation,
        )
