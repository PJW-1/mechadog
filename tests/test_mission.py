"""운용 모드 검증 (WBS 3.4.4 · FR-11 · ADR-33).

**이 파일이 지키는 것은 «한 순찰 = 한 모드» 다.** 모드가 하는 일은 사건을 거르는
것 하나뿐이라 구현은 짧지만, 틀리면 **같은 사람을 침입자와 작업자로 동시에 다루는**
상태로 돌아간다 — ADR-33 이 분리한 바로 그 충돌이다.

그래서 세 가지를 전수로 못 박는다.

1. **모드 × 사건** — FR-11.1 표를 시험이 **따로 적어** 구현과 대조한다.
2. **전환 조건** — 13개 상태 전부에서 `IDLE`·`MANUAL` 만 받는지 (FR-11.3).
3. **안전 상태 보존** — 전환이 L3·F 를 풀지 않는지 (FR-11.4).
"""

from __future__ import annotations

import pytest

from host.behavior import mission as mission_mod
from host.behavior.escalation import Escalation, Level
from host.behavior.fsm import DIRECTIVES, Event
from host.behavior.mission import (
    ENABLED,
    FEATURES,
    MODES,
    REQUIRES,
    Mission,
    ModeError,
    available_modes,
)

T0 = 1_000_000

#: **FR-11.1 표를 시험이 따로 적는다.** 구현에서 `ENABLED` 를 가져와 견주면 자기
#: 자신을 비교하는 꼴이라 표가 틀려도 통과한다 — `test_fsm.py` 가 전이표를 문서와
#: 대조하는 것과 같은 정신이다. 값은 **그 모드가 만들 수 있는 게이트 대상 사건**이다.
EXPECTED: dict[str, set[str]] = {
    # 경비 — 인증을 켜고 PPE·변화를 끈다. 추종은 한다 (FR-3.5).
    "guard": {"AUTH_REQUIRED", "AUTH_OK", "AUTH_FAILED", "TARGET_OFF_CENTER"},
    # 공장 — 변화 감지와 보호구 판정을 켜고 인증을 끈다.
    "factory": {
        "ZONE_ARRIVED",
        "ZONE_CLEAR",
        "ZONE_CHANGED",
        "PPE_VIOLATION",
        "PPE_SETTLED",
        "TARGET_OFF_CENTER",
    },
}

#: 어느 기능에도 걸리지 않는 사건. **모든 모드에 공통이다** (FR-11.1).
COMMON: set[str] = {event.name for event in Event} - {
    name for events in FEATURES.values() for name in events
}


@pytest.fixture
def any_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """선행 기능 검사를 끈다 — **게이트 표 자체**를 보는 시험용 (FR-11.1).

    ⚠️ 실제 `REQUIRES` 를 그대로 쓰면 `3.7.3` 이 들어오는 날 표 시험의
    통과 여부가 바뀐다. 선행 기능 규칙(FR-11.7)은 아래에서 따로 본다.
    """
    monkeypatch.setattr(mission_mod, "REQUIRES", dict.fromkeys(MODES, ()))


# ── 표 자체 ────────────────────────────────────────────────
def test_feature_table_uses_real_event_names() -> None:
    """**사건을 문자열로 두었으므로 오타가 조용히 통과한다.**

    `FEATURES` 의 이름 하나가 틀리면 그 사건은 어느 기능에도 속하지 않게 되고,
    `allows()` 는 *"공통 사건"* 으로 보아 **모든 모드에서 통과시킨다.** 경비 모드가
    PPE 사건을 받는 것이 오타 한 글자로 생긴다는 뜻이다.
    """
    names = {event.name for event in Event}
    for feature, events in FEATURES.items():
        unknown = events - names
        assert not unknown, f"{feature} 에 규약에 없는 사건: {sorted(unknown)}"


def test_every_mode_enables_only_known_features() -> None:
    """`ENABLED` 가 `FEATURES` 에 없는 기능을 켜면 그 줄은 아무 일도 하지 않는다."""
    assert set(ENABLED) == set(MODES)
    for mode, features in ENABLED.items():
        unknown = features - set(FEATURES)
        assert not unknown, f"{mode} 가 모르는 기능을 켠다: {sorted(unknown)}"


def test_every_mode_declares_requirements() -> None:
    """선행 기능 표에 빠진 모드가 있으면 그 모드는 **검사 없이 열린다** (FR-11.7)."""
    assert set(REQUIRES) == set(MODES)


def test_default_mode_is_guard() -> None:
    """**기본은 경비다** (FR-11.2). 유일하게 선행 기능이 없는 모드이기도 하다."""
    assert mission_mod.DEFAULT == "guard"
    assert REQUIRES["guard"] == ()


# ── 모드 × 사건 전수 (FR-11.1) ─────────────────────────────
@pytest.mark.usefixtures("any_mode")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("event", list(Event), ids=lambda e: e.name)
def test_every_mode_event_pair_matches_the_requirement_table(mode: str, event: Event) -> None:
    """28개 사건 × 3개 모드를 전수로 돌린다.

    기대값은 위 `EXPECTED`(모드가 켜는 것) 와 `COMMON`(누구도 끄지 않는 것)의 합이다.
    """
    allowed = event.name in COMMON or event.name in EXPECTED[mode]
    assert Mission({"mission": {"mode": mode}}).allows(event.name) is allowed


@pytest.mark.usefixtures("any_mode")
@pytest.mark.parametrize("mode", MODES)
def test_blocked_set_is_the_complement_of_the_enabled_features(mode: str) -> None:
    """`blocked()` 가 화면·로그에 그대로 쓰이므로 표와 어긋나면 설명이 거짓이 된다."""
    gated = {name for events in FEATURES.values() for name in events}
    assert Mission({"mission": {"mode": mode}}).blocked() == gated - EXPECTED[mode]


@pytest.mark.usefixtures("any_mode")
def test_common_events_are_never_gated() -> None:
    """**순찰·스캔·회피·수동·안전은 모든 모드 공통이다.**

    이것이 비면 표를 잘못 쓴 것이다 — 게이트가 전부를 덮으면 모드가 아니라 스위치다.
    """
    assert {"START_PATROL", "ESTOP", "ONBOARD_FAILSAFE", "PERSON_FOUND"} <= COMMON
    for mode in MODES:
        gate = Mission({"mission": {"mode": mode}})
        assert all(gate.allows(name) for name in COMMON)


# ── 기동 거부 (FR-11.2 · FR-11.7) ──────────────────────────
@pytest.mark.usefixtures("any_mode")
@pytest.mark.parametrize("value", ["", "safety", "GUARD", "factory ", "unknown"])
def test_unknown_mode_refuses_to_start(value: str) -> None:
    """모르는 값이면 기본값으로 떨어지지 않고 **기동을 거부한다** (WBS 3.4.4 ①).

    ⚠️ `safety` 가 여기 있는 것은 의도다 — ADR-33 개정 전의 옛 이름이라 문서를
    옛 판으로 읽은 사람이 실제로 적을 수 있는 값이다.
    """
    with pytest.raises(ModeError):
        Mission({"mission": {"mode": value}})


@pytest.mark.usefixtures("any_mode")
def test_mode_argument_beats_the_config() -> None:
    """`--mode` 가 `config.yaml` 을 덮어쓴다 (FR-11.2)."""
    assert Mission({"mission": {"mode": "guard"}}, mode="factory").mode == "factory"


@pytest.mark.usefixtures("any_mode")
def test_missing_section_falls_back_to_the_default() -> None:
    """설정에 절이 없어도 경비로 뜬다 — 시험·도구가 빈 설정을 넘기는 자리가 있다."""
    assert Mission({}).mode == "guard"


def test_mode_without_its_implementation_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**이름만 있는 모드를 켜지 않는다** (FR-11.7 · ADR-33 운용규칙 7).

    판정기 없이 공장 모드를 켜면 로봇이 사람 앞에 서서 아무 판정도 내지 못한다.
    """
    monkeypatch.setattr(mission_mod, "REQUIRES", {**REQUIRES, "factory": ("host.nonexistent",)})
    with pytest.raises(ModeError, match="host.nonexistent"):
        Mission({"mission": {"mode": "factory"}})
    assert "factory" not in available_modes()


def test_available_modes_hides_what_cannot_be_chosen(monkeypatch: pytest.MonkeyPatch) -> None:
    """관제 화면이 **고를 수 없는 것을 버튼으로 내놓지 않게** 한다."""
    monkeypatch.setattr(mission_mod, "REQUIRES", {"guard": (), "factory": ("host.nope",)})
    assert available_modes() == ("guard",)


def test_requirement_probe_survives_a_missing_parent_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """부모 패키지 자체가 없어도 **예외가 아니라 «없다»** 여야 한다.

    `find_spec` 은 부모를 먼저 import 하므로 그대로 두면 `ModuleNotFoundError` 가
    기동 경로로 샌다.
    """
    monkeypatch.setattr(mission_mod, "REQUIRES", {"guard": ("host.no_such_pkg.thing",)})
    assert mission_mod.missing_requirements("guard") == ("host.no_such_pkg.thing",)


# ── 전환 (FR-11.3) ─────────────────────────────────────────
@pytest.mark.usefixtures("any_mode")
@pytest.mark.parametrize("state", sorted(DIRECTIVES))
def test_mode_switch_is_accepted_only_while_standing_still(state: str) -> None:
    """13개 상태 전수 — **`IDLE`·`MANUAL` 에서만 받는다.**

    대응 중에 판정 규칙이 바뀌면 진행 중인 인증·자세 시퀀스가 의미를 잃는다.
    """
    gate = Mission({"mission": {"mode": "guard"}})
    refused = gate.switch("factory", state=state)
    if state in {"IDLE", "MANUAL"}:
        assert refused is None and gate.mode == "factory"
    else:
        assert refused is not None and gate.mode == "guard"


@pytest.mark.usefixtures("any_mode")
def test_refusal_explains_itself() -> None:
    """**사유를 돌려준다** (FR-11.3). 조용히 무시하면 조작자는 버튼 고장으로 읽는다."""
    gate = Mission({"mission": {"mode": "guard"}})
    assert "PATROL" in (gate.switch("factory", state="PATROL") or "")
    assert "sentry" in (gate.switch("sentry", state="IDLE") or "")


@pytest.mark.usefixtures("any_mode")
def test_switching_to_the_same_mode_is_a_no_op() -> None:
    """같은 모드로 바꾸는 것은 거절이 아니다 — 화면이 두 번 눌릴 수 있다."""
    gate = Mission({"mission": {"mode": "guard"}})
    assert gate.switch("guard", state="IDLE") is None
    assert gate.mode == "guard"


def test_switch_refuses_a_mode_without_its_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """선행 기능 검사는 **기동 때와 전환 때 모두** 돈다 (FR-11.7)."""
    monkeypatch.setattr(mission_mod, "REQUIRES", {**REQUIRES, "factory": ("host.nope",)})
    gate = Mission({"mission": {"mode": "guard"}})
    assert "host.nope" in (gate.switch("factory", state="IDLE") or "")
    assert gate.mode == "guard"


# ── 안전 상태 보존 (FR-11.4) ───────────────────────────────
@pytest.mark.usefixtures("any_mode")
@pytest.mark.parametrize("level", [Level.L3, Level.F])
def test_mode_switch_does_not_clear_a_latched_level(cfg: dict, level: Level) -> None:
    """⚠️ **전환이 경보를 지우면 확인 없는 해제 경로가 된다** (FR-11.4 · ADR-26).

    지금 `Mission` 은 `Escalation` 을 아예 모르므로 이 시험은 자명하게 통과한다 —
    **그것이 요점이다.** 언젠가 누가 두 축을 묶으면 여기서 걸린다.
    """
    esc = Escalation(cfg)
    esc.raise_to(level, reason="test", now_ms=T0)
    gate = Mission({"mission": {"mode": "guard"}})

    assert gate.switch("factory", state="IDLE") is None
    assert esc.level is level
    assert esc.latched
