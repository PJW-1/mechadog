"""단계적 자세 상승 검증 (WBS 3.5.7 · FR-9.2.2).

하드웨어가 필요 없다. 입력은 bbox 와 시각이고 출력은 «무엇을 할지» 하나다.

완료 기준이 넷이다 — **단계 순서**, **매 단계 재검사**, **정지한 대상만**(FR-9.2.0),
**이탈 시 중단·재시도 한도**(FR-9.2.4).
"""

from __future__ import annotations

import pytest

from host.behavior.posture import RETURN, PostureEscalation

FRAME_H = 480
#: 위 경계에 닿은 박스 — 머리가 잘렸다.
CLIPPED = (200.0, 0.0, 300.0, 400.0)
#: 머리가 보이는 박스.
CLEAR = (200.0, 120.0, 300.0, 400.0)


def _posture(cfg: dict, **ppe: object) -> PostureEscalation:
    merged = {
        "posture": dict(cfg["posture"]),
        "vision": {"ppe": dict(cfg["vision"]["ppe"]) | ppe},
    }
    return PostureEscalation(merged)


def _settle(cfg: dict) -> int:
    return int(cfg["posture"]["settle_ms"])


#: ⚠️ **정지는 «이동량» 으로 재므로 `static_frames + 1` 번 봐야 성립한다.** 첫 관측에는
#: 견줄 이전 중심이 없다. 시험이 이 하나를 빠뜨리면 «움직이는 중» 이 돌아온다.
def _still_frames(cfg: dict) -> int:
    return int(cfg["vision"]["ppe"]["static_frames"]) + 1


def _still(p: PostureEscalation, box, *, at_ms: int, frames: int, track_id: int = 1):
    """같은 자리에 `frames` 번 보인다. 마지막 판단을 돌려준다."""
    out = None
    for i in range(frames):
        out = p.update(box=box, frame_height=FRAME_H, track_id=track_id, now_ms=at_ms + i)
    return out


# ── 단계 순서와 재검사 ───────────────────────────────────────────


def test_clipping_walks_the_steps_in_order(cfg: dict) -> None:
    """① pitch_up → ② sit → ③ back_off. **순서가 곧 비용 순서다.**"""
    p = _posture(cfg)
    settle = _settle(cfg)
    assert _still(p, CLIPPED, at_ms=0, frames=_still_frames(cfg)).step == "pitch_up"
    assert p.update(box=CLIPPED, frame_height=FRAME_H, track_id=1, now_ms=settle + 10).step == "sit"
    assert (
        p.update(box=CLIPPED, frame_height=FRAME_H, track_id=1, now_ms=settle * 2 + 20).step
        == "back_off"
    )


def test_a_step_that_clears_the_view_stops_the_sequence(cfg: dict) -> None:
    """⚠️ **매 단계 재검사가 요점이다.** 한 단계로 풀리면 더 올리지 않는다 —
    ③ 은 로봇을 움직이므로 필요 없는데 올리면 그만큼 느리고 위험하다."""
    p = _posture(cfg)
    assert _still(p, CLIPPED, at_ms=0, frames=_still_frames(cfg)).step == "pitch_up"
    out = p.update(box=CLEAR, frame_height=FRAME_H, track_id=1, now_ms=_settle(cfg) + 10)
    assert out.step == RETURN, "시야가 확보되면 기본 자세로 돌아간다"
    assert p.step is None


def test_nothing_is_sent_while_the_pose_is_still_arriving(cfg: dict) -> None:
    """보간이 끝나기 전에 다시 보면 «아직 잘린다» 가 나와 단계를 헛되이 올린다."""
    p = _posture(cfg)
    assert _still(p, CLIPPED, at_ms=0, frames=_still_frames(cfg)).step == "pitch_up"
    mid = _settle(cfg) // 2
    assert p.update(box=CLIPPED, frame_height=FRAME_H, track_id=1, now_ms=mid).step is None


def test_a_clear_view_never_starts_the_sequence(cfg: dict) -> None:
    p = _posture(cfg)
    assert _still(p, CLEAR, at_ms=0, frames=_still_frames(cfg)).step is None
    assert p.step is None


# ── 정지한 대상만 (FR-9.2.0) ─────────────────────────────────────


def test_a_moving_target_does_not_start_the_sequence(cfg: dict) -> None:
    """⚠️ **움직이는 대상에 올리면 루프가 된다.** 로봇 10~30cm/s 대 사람 120~150cm/s 라
    한 바퀴마다 대상이 더 멀어진다."""
    p = _posture(cfg)
    jump = float(cfg["vision"]["ppe"]["static_threshold_px"]) + 10
    out = None
    for i in range(8):
        box = (200.0 + i * jump, 0.0, 300.0 + i * jump, 400.0)
        out = p.update(box=box, frame_height=FRAME_H, track_id=1, now_ms=i)
    assert out is not None and out.step is None
    assert p.step is None


def test_the_target_must_hold_still_for_the_configured_frames(cfg: dict) -> None:
    """한 프레임 조용했다고 정지가 아니다 — `static_frames` 만큼 이어져야 한다."""
    need = _still_frames(cfg)
    p = _posture(cfg)
    assert _still(p, CLIPPED, at_ms=0, frames=need - 1).step is None, "하나 모자라면 아직이다"
    assert p.update(box=CLIPPED, frame_height=FRAME_H, track_id=1, now_ms=99).step == "pitch_up"


# ── 이탈과 재시도 한도 (FR-9.2.4) ────────────────────────────────


def test_losing_the_target_aborts_back_to_neutral(cfg: dict) -> None:
    p = _posture(cfg)
    assert _still(p, CLIPPED, at_ms=0, frames=_still_frames(cfg)).step == "pitch_up"
    out = p.update(box=None, frame_height=FRAME_H, track_id=1, now_ms=_settle(cfg) + 10)
    assert out.step == RETURN
    assert p.step is None
    assert p.retries == 1, "이탈은 재시도를 한 번 쓴다"


def test_losing_a_target_we_never_posed_for_sends_nothing(cfg: dict) -> None:
    """⚠️ 헛된 `ACTION` 은 `loop()` 를 1초 막는다. 잡은 적이 없으면 되돌릴 것도 없다."""
    p = _posture(cfg)
    out = p.update(box=None, frame_height=FRAME_H, track_id=1, now_ms=0)
    assert out.step is None
    assert p.retries == 0


def test_a_clear_view_does_not_spend_a_retry(cfg: dict) -> None:
    """되돌리기가 전부 실패는 아니다 — 시야를 얻어서 내려오는 것은 성공이다."""
    p = _posture(cfg)
    _still(p, CLIPPED, at_ms=0, frames=_still_frames(cfg))
    p.update(box=CLEAR, frame_height=FRAME_H, track_id=1, now_ms=_settle(cfg) + 10)
    assert p.retries == 0


def test_retries_run_out_and_report_undetermined(cfg: dict) -> None:
    """**판정 실패도 결과다.** 적지 않으면 같은 상황이 고장으로 보인다."""
    limit = int(cfg["vision"]["ppe"]["max_posture_retries"])
    p = _posture(cfg)
    at = 0
    for _ in range(limit + 1):
        _still(p, CLIPPED, at_ms=at, frames=_still_frames(cfg))
        p.update(box=None, frame_height=FRAME_H, track_id=1, now_ms=at + 50)
        at += 1000
    out = _still(p, CLIPPED, at_ms=at, frames=_still_frames(cfg))
    assert out.undetermined, "한도를 넘으면 미판정으로 보고한다"
    assert out.step is None, "포기했으면 더 올리지 않는다"


def test_a_new_track_id_gets_its_own_retries(cfg: dict) -> None:
    """⚠️ **다른 사람이다.** 앞사람 몫을 물려받으면 시도도 못 해보고 미판정이 된다."""
    p = _posture(cfg)
    _still(p, CLIPPED, at_ms=0, frames=_still_frames(cfg))
    p.update(box=None, frame_height=FRAME_H, track_id=1, now_ms=50)
    assert p.retries == 1
    assert _still(p, CLIPPED, at_ms=1000, frames=_still_frames(cfg), track_id=2).step == "pitch_up"
    assert p.retries == 0


# ── 설정 오류는 조용히 넘어가지 않는다 ───────────────────────────


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("posture", "escalation_steps", []),
        ("posture", "escalation_steps", ["stand_two_legs"]),
        ("posture", "settle_ms", 0),
        ("ppe", "static_frames", 0),
    ],
)
def test_impossible_config_is_rejected(cfg: dict, section: str, key: str, value: object) -> None:
    merged = {
        "posture": dict(cfg["posture"]),
        "vision": {"ppe": dict(cfg["vision"]["ppe"])},
    }
    (merged["posture"] if section == "posture" else merged["vision"]["ppe"])[key] = value
    with pytest.raises(ValueError):
        PostureEscalation(merged)


def test_zero_frame_height_is_rejected(cfg: dict) -> None:
    p = _posture(cfg)
    with pytest.raises(ValueError):
        p.update(box=CLIPPED, frame_height=0, track_id=1, now_ms=0)
