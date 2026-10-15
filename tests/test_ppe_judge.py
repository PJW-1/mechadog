"""보호구 판정 단독 검증 — `PpeJudge` (FR-9 · ADR-42).

틱을 거치는 시나리오(적합·위반·판정불가·쓰러짐 보류)는 `test_runtime.py` 에 있다. 여기서는
런타임으로 닿지 않던 분기만 본다 — 후진 속도를 아는 물러서기와 자세를 푼 뒤의 순찰 대기.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from host.behavior.commander import Commander
from host.behavior.mission import Mission
from host.behavior.posture import PostureDecision
from host.behavior.ppe_judge import PpeJudge
from host.vision.ppe_detector import OK, UNDETERMINED

T0 = 1_000_000
#: 공장 모드의 선행 기능 검사를 연다 — 판정은 공장 모드에서만 돈다.
pytestmark = pytest.mark.usefixtures("unlock_modes")


def _judge(cfg: dict, *, reverse_mm_per_sec: float | None = None) -> PpeJudge:
    config = deepcopy(cfg)
    config["gait_calibration"] = {"reverse_mm_per_sec": reverse_mm_per_sec}
    return PpeJudge(
        config,
        behavior=SimpleNamespace(state="ALERT"),
        mission=Mission(config, mode="factory"),
        escalation=SimpleNamespace(settle_ppe=lambda _now_ms: None),
        fall=SimpleNamespace(suspected=False),
        commander=Commander(),
        apply=lambda _event, _now_ms: True,
        record=lambda *_args: None,
    )


def _frame() -> SimpleNamespace:
    """추적 1번이 판정 중(아직 모름)인 프레임."""
    verdict = SimpleNamespace(
        track_id=1,
        clipped=False,
        state=UNDETERMINED,
        confirmed=False,
        reason="판정 중",
        required=("helmet", "vest"),
    )
    track = SimpleNamespace(track_id=1, box=(100, 200, 200, 400), height=200)
    return SimpleNamespace(ppe=verdict, tracks=(track,), frame_height=480)


def _decide(judge: PpeJudge, step: str) -> None:
    judge._posture.update = lambda **_kw: PostureDecision(step, "시험")


def test_zone_change_ignores_old_result_and_vest_only_needs_no_head_pose(cfg):
    judge = _judge(cfg)
    judge.set_requirements(("vest",))
    frame = _frame()
    frame.ppe.state = OK
    judge.judge(frame, T0)
    assert not judge.is_done(1)
    frame.ppe.required = ("vest",)
    judge._posture.update = lambda **_kw: pytest.fail("vest-only must not request head posture")
    judge.judge(frame, T0 + 1)
    assert judge.is_done(1)


def test_a_measured_reverse_backs_off_after_standing_up(cfg: dict) -> None:
    """후진 속도를 알면 서는 시간 뒤부터 `back_off_mm` 만큼 후진하고, 그동안 판정을 멈춘다."""
    judge = _judge(cfg, reverse_mm_per_sec=100.0)
    commander = Commander()
    _decide(judge, "back_off")
    judge.judge(_frame(), T0)
    start = T0 + max(int(cfg["posture"]["settle_ms"]), 1000)
    end = start + int(float(cfg["posture"]["back_off_mm"]) / 100.0 * 1000)

    judge.alert_sequence(commander, start - 1)
    assert commander.intent.fields == {"step": 0.0, "angle": 0.0}, "서기 전에는 걷지 않는다"
    judge.alert_sequence(commander, start)
    assert commander.intent.fields["step"] == -float(cfg["gait"]["step_length_mm"])

    _decide(judge, "pitch_up")
    judge.judge(_frame(), start)
    assert not judge._commander.has_pending("POSE"), "후진 중에는 판정 자세를 올리지 않는다"

    judge.alert_sequence(commander, end)
    assert commander.intent.fields == {"step": 0.0, "angle": 0.0}
    judge.judge(_frame(), end)
    assert judge._commander.has_pending("POSE"), "후진이 끝나면 판정을 잇는다"


def test_releasing_the_pose_holds_the_patrol_until_it_stands(cfg: dict) -> None:
    """판정 자세를 풀면 서는 동안(`settle_ms`, 최소 1초) 순찰이 걷지 않는다."""
    judge = _judge(cfg)
    _decide(judge, "pitch_up")
    judge.judge(_frame(), T0)
    assert judge.pose_held
    judge.note_time(T0 + 10)
    assert judge.return_pose()
    until = T0 + 10 + max(int(cfg["posture"]["settle_ms"]), 1000)
    assert judge.halts_patrol(until - 1)
    assert not judge.halts_patrol(until)
    judge.reset()
    assert not judge.halts_patrol(until - 1), "모드 전환은 순찰 대기도 지운다"
