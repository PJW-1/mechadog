"""TRACK 락온 제어기 검증 (WBS 3.5.4 · FR-3.5).

⚠️ 파일명이 `test_behavior_tracker` 인 이유 — `tests/test_tracker.py` 는 이미
`host/vision/tracker.py`(다중 인원 추적 · WBS 3.3.4)가 쓰고 있다. **이름이 같은
모듈이 둘이다** — 이쪽은 `host/behavior/tracker.py`(락온 제어)이고 저쪽은 비전의
다중 인원 추적이다. 시험 파일 이름으로 구분한다.

완료 기준이 둘이다 — **x편차 비례 선회**와 **데드존으로 미세 진동 억제**. 앞엣것은
부호와 크기를, 뒤엣것은 데드존 안에서 명령이 나가지 않는다는 것을 본다.

하드웨어가 필요 없다. 입력은 픽셀 좌표뿐이고 출력은 숫자 두 개다.
"""

from __future__ import annotations

import pytest

from host.behavior.tracker import LockOnTracker, TrackCommand

FRAME_WIDTH = 640
MIDPOINT = FRAME_WIDTH / 2


def _tracker(cfg: dict, *, deadzone: float | None = None) -> LockOnTracker:
    merged = {
        "fsm": dict(cfg["fsm"]),
        "gait": dict(cfg["gait"]),
    }
    if deadzone is not None:
        merged["fsm"]["track_deadzone_px"] = deadzone
    return LockOnTracker(merged)


# ── 데드존 — 미세 진동 억제 ──────────────────────────────────────


def test_dead_centre_holds_still_and_reports_centred(cfg):
    t = _tracker(cfg)
    out = t.update(MIDPOINT, FRAME_WIDTH)
    assert out == TrackCommand(step=0.0, angle=0.0, centered=True, deviation_px=0.0)


@pytest.mark.parametrize("offset", [0.0, 1.0, 20.0, 39.9, 40.0])
def test_no_command_leaves_inside_the_deadzone(cfg, offset):
    """**이것이 미세 진동 억제다.** 경계값(40px)까지 포함해 정지를 유지한다."""
    t = _tracker(cfg)
    for signed in (offset, -offset):
        out = t.update(MIDPOINT + signed, FRAME_WIDTH)
        assert out.centered is True
        assert out.step == 0.0
        assert out.angle == 0.0


def test_steering_always_carries_a_stride(cfg):
    """제자리 회전이 불가하므로(DR-11) 조향에는 반드시 보폭이 따라붙는다."""
    t = _tracker(cfg)
    out = t.update(MIDPOINT + 41, FRAME_WIDTH)
    assert out.centered is False
    assert out.step == abs(float(cfg["gait"]["step_length_mm"]))
    assert out.step > 0


# ── 부호 — 반대로 가면 타겟을 놓친다 ─────────────────────────────


def test_target_on_the_right_turns_right_with_a_negative_angle(cfg):
    """화면 x 는 오른쪽이 양수, 조향은 양수가 좌회전 — **부호가 반대다**."""
    t = _tracker(cfg)
    out = t.update(MIDPOINT + 200, FRAME_WIDTH)
    assert out.deviation_px > 0
    assert out.angle < 0


def test_target_on_the_left_turns_left_with_a_positive_angle(cfg):
    t = _tracker(cfg)
    out = t.update(MIDPOINT - 200, FRAME_WIDTH)
    assert out.deviation_px < 0
    assert out.angle > 0


def test_left_and_right_are_mirror_images(cfg):
    t = _tracker(cfg)
    right = t.update(MIDPOINT + 150, FRAME_WIDTH)
    left = t.update(MIDPOINT - 150, FRAME_WIDTH)
    assert right.angle == pytest.approx(-left.angle)
    assert right.step == left.step


# ── 비례 — 크기가 편차를 따라간다 ────────────────────────────────


def test_larger_deviation_steers_harder(cfg):
    t = _tracker(cfg)
    angles = [abs(t.update(MIDPOINT + d, FRAME_WIDTH).angle) for d in (60, 120, 240)]
    assert angles[0] < angles[1] < angles[2]


def test_angle_does_not_jump_at_the_deadzone_edge(cfg):
    """데드존을 뺀 나머지를 비례 구간으로 쓰므로 경계에서 0 에서 시작한다.

    그대로 비례시켰다면 경계 직후에 이미 상당한 각이 나온다 — 그게 튀는 것이다.
    """
    t = _tracker(cfg)
    just_outside = t.update(MIDPOINT + t.deadzone_px + 0.01, FRAME_WIDTH)
    assert just_outside.centered is False
    assert abs(just_outside.angle) < 0.05


def test_frame_edge_saturates_at_the_configured_maximum(cfg):
    t = _tracker(cfg)
    limit = abs(float(cfg["gait"]["turn_angle_deg"]))
    edge = t.update(FRAME_WIDTH, FRAME_WIDTH)
    assert abs(edge.angle) == pytest.approx(limit)
    # 화면 밖 좌표가 들어와도 규약 범위를 넘기지 않는다.
    beyond = t.update(FRAME_WIDTH * 3, FRAME_WIDTH)
    assert abs(beyond.angle) == pytest.approx(limit)


def test_steering_angle_stays_within_the_protocol_range(cfg):
    """`gait.turn_angle_deg` 주석의 -30~30 을 벗어나면 로봇이 거부한다."""
    t = _tracker(cfg)
    for x in (0, 1, 100, 320, 500, 639, 640):
        assert -30.0 <= t.update(x, FRAME_WIDTH).angle <= 30.0


# ── 설정 오류는 조용히 넘어가지 않는다 ───────────────────────────


def test_zero_frame_width_is_rejected(cfg):
    t = _tracker(cfg)
    with pytest.raises(ValueError):
        t.update(MIDPOINT, 0)


def test_deadzone_wider_than_half_the_frame_is_rejected(cfg):
    """추종이 영원히 성립하지 않는 설정이다. 정지로 위장하지 않는다."""
    t = _tracker(cfg, deadzone=MIDPOINT)
    with pytest.raises(ValueError):
        t.update(MIDPOINT + 100, FRAME_WIDTH)


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("fsm", "track_deadzone_px", -1),
        ("gait", "step_length_mm", 0),
        ("gait", "turn_angle_deg", 0),
    ],
)
def test_impossible_config_is_rejected_at_construction(cfg, section, key, value):
    merged = {"fsm": dict(cfg["fsm"]), "gait": dict(cfg["gait"])}
    merged[section][key] = value
    with pytest.raises(ValueError):
        LockOnTracker(merged)


# ── 상태를 들지 않는다 ───────────────────────────────────────────


def test_same_input_gives_same_output(cfg):
    """시간·이력에 의존하지 않는다. 대상 상실은 FR-3.7 타이머 소관이다."""
    t = _tracker(cfg)
    first = t.update(MIDPOINT + 137, FRAME_WIDTH)
    t.update(MIDPOINT - 300, FRAME_WIDTH)
    assert t.update(MIDPOINT + 137, FRAME_WIDTH) == first
