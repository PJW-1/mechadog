"""사람 판정 게이트 검증 (WBS 3.3.3 · FR-3.2).

**시간을 만들지 않으므로 가상 시간으로 전수 검증된다.** `now_ms` 를 받는 설계가
여기서 값을 한다 — 실기 14초를 기다리지 않고 같은 판정을 재현한다.

⚠️ 핵심 시험은 **추론률을 바꿔도 판정 기준이 흔들리지 않는가** 다. 프레임 수로
정의했을 때 같은 "3프레임" 이 7fps 에서 429ms, 25fps 에서 120ms 였다.
"""

from __future__ import annotations

import pytest

from host.common.config import ConfigError, load_config, validate_base_config
from host.vision.detector import Detection
from host.vision.person import PersonGate


@pytest.fixture
def cfg() -> dict:
    return load_config("mechdog-01")


def _person(score: float = 0.9) -> Detection:
    return Detection(label="person", score=score, box=(10.0, 20.0, 110.0, 320.0))


def _other(label: str = "bed") -> Detection:
    return Detection(label=label, score=0.8, box=(0.0, 0.0, 50.0, 50.0))


def _feed(gate: PersonGate, pattern: str, *, fps: int, start_ms: int = 1000) -> list[bool]:
    """`pattern` 의 `#` 는 사람이 보인 관측, `.` 는 안 보인 관측.

    관측 간격을 fps 로 계산한다 — **추론률을 바꿔도 기준이 같은지**가 이 시험의 목적이다.
    """
    step = round(1000 / fps)
    out = []
    for i, ch in enumerate(pattern):
        dets = [_person()] if ch == "#" else []
        out.append(gate.observe(start_ms + i * step, dets).present)
    return out


# ── 기본 판정 ───────────────────────────────────────────────
def test_single_detection_is_not_enough(cfg: dict) -> None:
    """⚠️ **단발 오검출로 ALERT 로 튀지 않는다** — 그것이 FR-3.2 의 목적이다."""
    gate = PersonGate(cfg)
    assert _feed(gate, "#..", fps=20) == [False, False, False]


def test_required_hits_confirm(cfg: dict) -> None:
    gate = PersonGate(cfg)
    seen = _feed(gate, "###", fps=20)
    assert seen == [False, False, True]
    assert gate.present is True


def test_hits_may_be_non_contiguous_within_the_window(cfg: dict) -> None:
    """⚠️ **연속이 아니어도 된다.** 창 안에 필요한 횟수가 있으면 사람이다.

    실기에서 걸어 지나가는 사람은 프레임마다 잡히지 않는다(343프레임 중 52%).
    "연속" 을 요구하면 **깜빡임 하나가 확인을 처음부터 다시 시작하게** 만든다.
    """
    gate = PersonGate(cfg)
    # 20fps(50ms) 로 #.#.# = 200ms 안에 3회 → 창 300ms 안이다
    assert _feed(gate, "#.#.#", fps=20)[-1] is True


def test_non_person_labels_are_ignored(cfg: dict) -> None:
    """실기 프레임에 `bed` 가 0.79 로 잡혔다 — 사람이 아니다."""
    gate = PersonGate(cfg)
    for i in range(6):
        result = gate.observe(1000 + i * 50, [_other(), _other("chair")])
    assert result.present is False
    assert result.hits == 0


def test_best_box_is_reported_for_tracking(cfg: dict) -> None:
    """FR-3.5 추종이 쓸 대표 박스를 함께 낸다."""
    gate = PersonGate(cfg)
    for i in range(3):
        result = gate.observe(1000 + i * 50, [_person(0.6), _person(0.91)])
    assert result.present is True
    assert result.best_score == pytest.approx(0.91)
    assert result.box is not None


# ── ⚠️ 추론률에 종속되지 않는다 ─────────────────────────────
@pytest.mark.parametrize("fps", [7, 10, 15, 20, 25])
def test_confirmation_takes_the_same_time_at_any_rate(cfg: dict, fps: int) -> None:
    """⚠️ **이것이 프레임 수를 버린 이유다.**

    `연속 3프레임` 은 7fps 에서 429ms, 25fps 에서 120ms 로 **3.5배** 달라졌다.
    시간 창으로 정의하면 어느 추론률에서도 확정에 걸리는 시간이 창 안에 머문다.
    """
    gate = PersonGate(cfg)
    step = round(1000 / fps)
    confirmed_at = None
    for i in range(40):
        now = 1000 + i * step
        if gate.observe(now, [_person()]).present:
            confirmed_at = now - 1000
            break
    assert confirmed_at is not None, f"{fps}fps 에서 확정되지 않았다"
    assert confirmed_at <= gate.window_ms, f"{fps}fps 에서 창({gate.window_ms}ms)보다 늦었다"


def test_rate_too_low_for_the_window_is_refused_at_load(cfg: dict) -> None:
    """⚠️ **창 안에 필요한 횟수가 들어갈 수 없으면 영원히 확정되지 않는다.**

    추론률과 창 길이가 함께 정하는 상한이라 둘 중 하나만 봐서는 잡히지 않는다.
    조용히 "사람을 못 찾는" 상태가 되므로 기동에서 막는다.
    """
    from copy import deepcopy

    broken = deepcopy(cfg)
    broken["vision"]["inference_fps"] = 5  # 300ms 창에 1.5회밖에 안 들어간다
    with pytest.raises(ConfigError, match="영원히 확정되지 않음"):
        validate_base_config(broken)


# ── ⚠️ 진입·해제 비대칭 ─────────────────────────────────────
def test_release_requires_an_empty_window(cfg: dict) -> None:
    """⚠️ **진입과 대칭으로 두면 경계에서 떨린다.**

    그 떨림이 ALERT↔PATROL 전이를 왕복시킨다. 그래서 해제는 창이 **완전히** 빌 때만이다.
    """
    gate = PersonGate(cfg)
    assert _feed(gate, "###", fps=20)[-1] is True
    # 히트가 3 → 1 로 줄어도 유지된다
    assert gate.observe(1200, []).present is True
    assert gate.observe(1250, []).present is True


def test_release_when_window_empties(cfg: dict) -> None:
    gate = PersonGate(cfg)
    _feed(gate, "###", fps=20)
    assert gate.present is True
    # 창(300ms)보다 멀리 떨어진 관측 하나로 이전 히트가 전부 밀려난다
    result = gate.observe(1000 + gate.window_ms + 200, [])
    assert result.present is False
    assert result.hits == 0
    assert result.changed is True


# ── 사건 발행은 바뀔 때만 ───────────────────────────────────
def test_changed_is_true_only_on_transitions(cfg: dict) -> None:
    """⚠️ 같은 값이 20Hz 로 반복되는데 매번 발행하면 FSM 이 같은 전이를 수백 번 본다."""
    gate = PersonGate(cfg)
    changes = []
    for i in range(10):
        changes.append(gate.observe(1000 + i * 50, [_person()]).changed)
    assert changes.count(True) == 1, "확정 순간 한 번만"
    assert changes[2] is True


def test_missing_observations_are_required_to_release(cfg: dict) -> None:
    """⚠️ **사람이 안 보인 관측도 넣어야 한다.**

    넣지 않으면 창이 오래된 히트만 담은 채 남아 사람이 사라진 뒤에도 확정이 유지된다.
    """
    gate = PersonGate(cfg)
    _feed(gate, "###", fps=20)  # 1000 · 1050 · 1100 에 히트
    assert gate.present is True
    # 관측을 넣지 않고 시간만 흐르면 게이트는 아무것도 모른다
    assert gate.present is True
    # ⚠️ **마지막 히트(1100) 기준으로 창을 벗어나야** 한다. 첫 히트(1000) 기준으로
    # 계산하면 1100 이 아직 창 안이라 해제되지 않는다 — 처음에 그렇게 써서 틀렸다.
    gate.observe(1100 + gate.window_ms + 1, [])
    assert gate.present is False


def test_reset_clears_the_window(cfg: dict) -> None:
    """스트림이 끊겼다 붙으면 이전 창을 이어 쓰지 않는다."""
    gate = PersonGate(cfg)
    _feed(gate, "###", fps=20)
    gate.reset()
    assert gate.present is False
    assert gate.last is None


# ── 실기 데이터 재현 ────────────────────────────────────────
#: 2026-09-10 실기 343프레임(25fps · 사람이 걸어서 통과)에서 임계 0.5 를 넘은
#: **연속 구간 길이**다. 단발 8개가 섞여 있고 경계의 2연속이 0개였다.
REAL_RUNS = (52, 38, 29, 15, 12, 6, 6, 5, 3, 3, 1, 1, 1, 1, 1, 1, 1, 1)


def test_real_walkthrough_singles_are_all_suppressed(cfg: dict) -> None:
    """⚠️ **실기의 단발 8개가 전부 막혀야 한다** — 그것이 이 조건의 존재 이유다."""
    gate = PersonGate(cfg)
    now = 1000
    step = 40  # 25fps 수신 간격
    suppressed = 0
    for length in REAL_RUNS:
        if length != 1:
            continue
        gate.reset()
        for i in range(length):
            gate.observe(now + i * step, [_person()])
        suppressed += 0 if gate.present else 1
    assert suppressed == 8, "단발 8개가 전부 막혀야 한다"


def test_real_walkthrough_true_runs_are_all_confirmed(cfg: dict) -> None:
    """⚠️ **실기의 진짜 구간 10개가 전부 인정돼야 한다.**

    실측이 추론률을 정했다 — 10fps 4개 · 20fps 8개 · **25fps 10개.** 20fps 에서
    놓치는 둘은 **120ms 짜리 짧은 검출**이고, 3회를 채우려면 관측 3개가 들어가야
    하는데 50ms 간격으로는 2개뿐이다. 수신률과 같게 두면 프레임을 버리지 않는다.

    ⚠️ 이 시험은 **`inference_fps` 를 낮추면 깨진다.** 그것이 의도다 — 값을 되돌리면
    무엇을 잃는지 여기서 즉시 드러난다.
    """
    fps = int(cfg["vision"]["inference_fps"])
    step = round(1000 / fps)
    true_runs = [r for r in REAL_RUNS if r >= 3]
    assert len(true_runs) == 10

    confirmed = 0
    for length in true_runs:
        gate = PersonGate(cfg)
        # 25fps 원자료의 길이를 현재 추론률의 관측 수로 환산한다
        samples = max(1, round(length * fps / 25))
        for i in range(samples):
            gate.observe(1000 + i * step, [_person()])
        confirmed += 1 if gate.present else 0
    assert confirmed == 10, f"{fps}fps 에서 {confirmed}/10 만 확정됐다"
