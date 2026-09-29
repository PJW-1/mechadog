"""경비 추종 단독 검증 — `TrackController` (FR-3.5 · ADR-40).

틱을 거치는 시나리오(접근·정지선·히스테리시스·조준 상한·고개 든 뒤)는 `test_runtime.py` 에
있다. 여기서는 공개 API 로만 닿는 조각 — 제자리 회전 두 단계, 재장전, 모드·쓰러짐 의심 조건,
설정 오류 한 번만 기록 — 을 본다.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from host.behavior.fsm import Event
from host.behavior.mission import Mission
from host.behavior.track_controller import TrackController
from host.common.logging_setup import PeriodicSummary

T0 = 1_000_000
WIDTH = 640


class _Sequence:
    def __init__(self) -> None:
        self.notes: list[tuple[float, float, int]] = []

    def note(self, step: float, angle: float, now_ms: int) -> None:
        self.notes.append((step, angle, now_ms))


def _controller(
    cfg: dict, *, mode: str = "guard", suspected: bool = False, state: str = "ALERT"
) -> tuple[TrackController, list[Event], _Sequence]:
    events: list[Event] = []
    sequence = _Sequence()
    behavior = SimpleNamespace(
        tracking=state in {"ALERT", "TRACK"}, sequence_for=lambda _state: sequence
    )

    def apply(event: Event, _now_ms: int) -> bool:
        events.append(event)
        return True

    controller = TrackController(
        cfg,
        behavior=behavior,
        mission=Mission(cfg, mode=mode),
        summary=PeriodicSummary(),
        apply=apply,
        fall_suspected=lambda: suspected,
    )
    return controller, events, sequence


def _frame(center_x: float, height: float = 200.0, width: int = WIDTH) -> SimpleNamespace:
    box = (center_x - 20.0, 100.0, center_x + 20.0, 100.0 + height)
    return SimpleNamespace(sighting=SimpleNamespace(box=box), frame_width=width)


def test_stop_line_spins_in_two_steps(cfg: dict) -> None:
    """정지선(초음파)에서는 걷지 않고 편차가 분기점 안이면 작은 각, 밖이면 큰 각으로 돈다."""
    fsm = cfg["fsm"]
    controller, _events, sequence = _controller(cfg)
    controller.note_distance(float(fsm["track_stop_dist_cm"]))
    small = float(fsm["track_deadzone_px"]) + 10.0
    large = float(fsm["track_turn_split_px"]) + 10.0
    controller.track(_frame(WIDTH / 2 + small), T0)
    controller.track(_frame(WIDTH / 2 - large), T0 + 100)
    assert sequence.notes == [
        (0.0, -float(fsm["track_turn_small_deg"]), T0),
        (0.0, float(fsm["track_turn_large_deg"]), T0 + 100),
    ]


def test_rearm_releases_engaged_and_the_stop_line(cfg: dict) -> None:
    """고개를 든 뒤에는 쫓지 않고, 재장전하면 정지선 전(걷는 접근)부터 다시 시작한다."""
    controller, events, sequence = _controller(cfg)
    controller.engage()
    assert controller.engaged
    controller.track(_frame(WIDTH / 2 + 60.0), T0)
    assert (events, sequence.notes) == ([], [])
    controller.rearm()
    assert not controller.engaged
    controller.track(_frame(WIDTH / 2 + 60.0), T0 + 100)
    assert events == [Event.TARGET_OFF_CENTER]
    step, _angle, _now = sequence.notes[-1]
    assert step > 0.0, "정지선 전에는 걸으며 접근한다"


@pytest.mark.usefixtures("unlock_modes")
def test_factory_tracks_only_while_a_fall_is_suspected(cfg: dict) -> None:
    """공장 모드는 사람을 쫓지 않고, 쓰러짐 의심일 때만 같은 조준·접근을 쓴다."""
    idle, idle_events, _ = _controller(cfg, mode="factory")
    assert not idle.tracks_person()
    idle.track(_frame(WIDTH / 2 + 60.0), T0)
    assert idle_events == []
    suspecting, events, _ = _controller(cfg, mode="factory", suspected=True)
    suspecting.track(_frame(WIDTH / 2 + 60.0), T0)
    assert events == [Event.TARGET_OFF_CENTER]


def test_unusable_deadzone_is_logged_once(cfg: dict, caplog: pytest.LogCaptureFixture) -> None:
    """데드존이 화면 반폭 이상이면 프레임을 버리고 `track_unusable` 을 한 번만 남긴다."""
    config = deepcopy(cfg)
    config["fsm"]["track_deadzone_px"] = WIDTH
    controller, events, sequence = _controller(config)
    controller.track(_frame(WIDTH / 2), T0)
    controller.track(_frame(WIDTH / 2), T0 + 100)
    assert (events, sequence.notes) == ([], [])
    assert caplog.text.count("track_unusable") == 1
