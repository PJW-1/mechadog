"""쓰러짐 규칙 판정 검증 (WBS 4.8.3 · FR-9 · ADR-35 대안 ⓓ).

**가상 시간으로 전수 검증된다** — `now_ms` 를 받으므로 3초를 실제로 기다리지 않는다.
`person.py` 의 다른 판정과 같은 규칙이다.

여기서 지키는 것은 넷이다.

    ① 실측 박스(세로 88 · 가로 286)를 «누움» 으로 읽는다
    ② 종횡비만으로 확정하지 않는다 — 정지가 이어져야 한다
    ③ 프레임이 아니라 시간으로 센다
    ④ 대상이 바뀌거나 사라지면 누적을 지운다
"""

from __future__ import annotations

import pytest

from host.vision.person import FallenGate

#: 실측 — 쓰러진 작업자의 박스 (2026-09-17 · YOLOX `person` 0.89).
FALLEN_BOX = (100.0, 200.0, 386.0, 288.0)  # 가로 286 · 세로 88 → 3.25
STANDING_BOX = (100.0, 100.0, 180.0, 300.0)  # 가로 80 · 세로 200 → 0.4
SITTING_BOX = (100.0, 100.0, 220.0, 240.0)  # 가로 120 · 세로 140 → 0.86


@pytest.fixture
def gate(cfg: dict) -> FallenGate:
    return FallenGate(cfg)


def _hold(gate: FallenGate, box, *, start: int, ms: int, step: int = 40, track: int = 1):
    """같은 자리에 `ms` 동안 머무르게 한다. 25fps = 40ms 주기."""
    verdict = None
    for offset in range(0, ms + 1, step):
        verdict = gate.observe(start + offset, box, track_id=track)
    return verdict


# ── ① 실측 박스를 읽는다 ────────────────────────────────────


def test_the_measured_fallen_box_is_confirmed(gate: FallenGate) -> None:
    """실측 세로 88 · 가로 286 — 종횡비 3.25."""
    verdict = _hold(gate, FALLEN_BOX, start=1000, ms=3000)
    assert verdict.fallen is True
    assert verdict.aspect == pytest.approx(286 / 88, rel=1e-3)


def test_a_standing_person_is_never_confirmed(gate: FallenGate) -> None:
    verdict = _hold(gate, STANDING_BOX, start=1000, ms=10_000)
    assert verdict.fallen is False
    assert verdict.candidate is False


def test_a_sitting_person_is_never_confirmed(gate: FallenGate) -> None:
    """⚠️ 앉은 자세를 쓰러짐으로 읽으면 시연 내내 헛경보가 난다."""
    verdict = _hold(gate, SITTING_BOX, start=1000, ms=10_000)
    assert verdict.fallen is False


# ── ② 종횡비만으로는 부족하다 ───────────────────────────────


def test_shape_alone_does_not_confirm(gate: FallenGate) -> None:
    """⚠️ 카메라에 붙은 사람·겹친 두 사람·팔 벌린 순간도 가로로 넓다."""
    verdict = gate.observe(1000, FALLEN_BOX, track_id=1)
    assert verdict.candidate is True, "모양은 맞으므로 후보이긴 하다"
    assert verdict.fallen is False, "한 프레임으로 확정하면 안 된다"


def test_movement_resets_the_hold(gate: FallenGate) -> None:
    """넘어진 뒤 버둥거리는 사람은 확정되지 않는다."""
    _hold(gate, FALLEN_BOX, start=1000, ms=2000)
    moved = (
        FALLEN_BOX[0] + 60,
        FALLEN_BOX[1],
        FALLEN_BOX[2] + 60,
        FALLEN_BOX[3],
    )
    verdict = gate.observe(3100, moved, track_id=1)
    assert verdict.fallen is False
    assert verdict.still_ms == 0, "움직였는데 누적이 남아 있다"


def test_confirmation_needs_the_full_window(gate: FallenGate) -> None:
    just_short = _hold(gate, FALLEN_BOX, start=1000, ms=2960)
    assert just_short.fallen is False
    assert gate.observe(1000 + 3000, FALLEN_BOX, track_id=1).fallen is True


# ── ③ 시간으로 센다 ────────────────────────────────────────


def test_the_window_is_time_not_frames(gate: FallenGate) -> None:
    """⚠️ 프레임으로 두면 같은 조건이 10fps 와 25fps 에서 다른 시간이 된다."""
    slow = FallenGate(
        {"vision": {"fallen": {"aspect_ratio": 1.5, "still_threshold_px": 20, "confirm_ms": 3000}}}
    )

    fast = _hold(gate, FALLEN_BOX, start=0, ms=3000, step=40)  # 25fps · 76프레임
    few = _hold(slow, FALLEN_BOX, start=0, ms=3000, step=200)  # 5fps · 16프레임
    assert fast.fallen is True
    assert few.fallen is True, "프레임 수가 적어도 시간이 찼으면 확정한다"


# ── ④ 누적을 지운다 ────────────────────────────────────────


def test_losing_the_box_clears_the_hold(gate: FallenGate) -> None:
    """⚠️ 남기면 사람이 사라졌다 다시 나타날 때 한 번 보고 확정하는 꼴이 된다."""
    _hold(gate, FALLEN_BOX, start=1000, ms=2960)
    assert gate.observe(3000, None).fallen is False
    assert gate.observe(3040, FALLEN_BOX, track_id=1).fallen is False


def test_a_different_target_does_not_inherit_the_hold(gate: FallenGate) -> None:
    """⚠️ 다른 사람의 정지가 이번 사람 몫을 채우면 안 된다."""
    _hold(gate, FALLEN_BOX, start=1000, ms=2960, track=1)
    verdict = gate.observe(4000, FALLEN_BOX, track_id=2)
    assert verdict.fallen is False
    assert verdict.still_ms == 0


def test_reset_clears_everything(gate: FallenGate) -> None:
    _hold(gate, FALLEN_BOX, start=1000, ms=2960)
    gate.reset()
    assert gate.observe(4000, FALLEN_BOX, track_id=1).fallen is False


# ── 사건은 한 번만 ─────────────────────────────────────────


def test_changed_fires_once_not_every_frame(gate: FallenGate) -> None:
    """⚠️ 쓰러진 사람은 계속 쓰러져 있다 — 매 프레임 사건을 올리면 안 된다."""
    _hold(gate, FALLEN_BOX, start=1000, ms=2960)
    first = gate.observe(4000, FALLEN_BOX, track_id=1)
    second = gate.observe(4040, FALLEN_BOX, track_id=1)
    assert (first.fallen, first.changed) == (True, True)
    assert (second.fallen, second.changed) == (True, False)


def test_recovery_is_also_a_change(gate: FallenGate) -> None:
    """일어나면 그것도 한 번 알린다."""
    _hold(gate, FALLEN_BOX, start=1000, ms=3000)
    stood = gate.observe(4100, STANDING_BOX, track_id=1)
    assert (stood.fallen, stood.changed) == (False, True)


# ── 설정 검증 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "value"),
    [("aspect_ratio", 1.0), ("aspect_ratio", 0.5), ("confirm_ms", 0)],
)
def test_impossible_settings_are_refused(cfg: dict, key: str, value: float) -> None:
    from copy import deepcopy

    broken = deepcopy(cfg)
    broken["vision"]["fallen"][key] = value
    with pytest.raises(ValueError, match=f"vision.fallen.{key}"):
        FallenGate(broken)
