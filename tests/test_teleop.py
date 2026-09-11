"""키보드 수동 조작 검증 (WBS 4.6.5 · FR-4.3).

키 입력 판정이 순수 함수이므로 **키보드도 로봇도 없이 전부 검증된다.**
콘솔 읽기(`read_keys`)만 바깥세상이며 거기에는 판단이 없다.
"""

import json

import pytest
from conftest import FakeClock

from host.behavior.commander import HALT, Commander
from host.behavior.fsm import Behavior, Event
from host.common.protocol import CommandDecoder, CommandEncoder
from tools.teleop import Teleop, resolve


def make(clock: FakeClock, release_ms: int = 600) -> Teleop:
    behavior = Behavior(Commander(CommandEncoder(clock=clock), period_ms=100))
    return Teleop(behavior, step_mm=60, turn_deg=20, release_ms=release_ms)


def last(lines: list[str]) -> dict:
    return json.loads(lines[-1])


#: ⚠️ **이 표가 뒤집힌 채로 시험에 고정돼 있었다** (2026-09-11 실물 조종에서 발견).
#:
#: `angle` 의 양수는 **반시계 = 로봇의 좌회전**인데(`docs/PROTOCOL.md` 부호 규약)
#: `right` 에 양수를 주고 있었다. 시험이 그 가정을 함께 못 박고 있었으므로 **시험도
#: 틀려 있었다** — 규약에 부호가 적혀 있지 않아 검증할 근거가 없었고, 펌웨어가
#: dry-run 이던 동안에는 서보가 돌지 않아 실물로도 드러날 수 없었다.
@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("up", (60, 0)),
        ("w", (60, 0)),
        ("down", (-60, 0)),
        ("s", (-60, 0)),
        # 좌 = 양수 (반시계)
        ("left", (0, 20)),
        ("a", (0, 20)),
        ("right", (0, -20)),
        ("d", (0, -20)),
        ("q", (60, 20)),
        ("e", (60, -20)),
    ],
)
def test_key_maps_to_step_and_angle(key: str, expected: tuple[float, float]) -> None:
    assert resolve(key, step_mm=60, turn_deg=20) == expected


def test_left_is_positive_and_right_is_negative() -> None:
    """부호 규약을 **단독으로** 못 박는다 — 표를 고칠 때 함께 봐야 한다.

    ⚠️ 양수 = 반시계 = 좌회전 (ROS REP-103 과 같은 규약). 2026-09-11 실물에서
    `angle=+20` 이 반시계로 도는 것을 확인했다.
    """
    left = resolve("left", step_mm=60, turn_deg=20)
    right = resolve("right", step_mm=60, turn_deg=20)
    assert left is not None and right is not None
    assert left[1] > 0, "좌회전은 양수여야 한다"
    assert right[1] < 0, "우회전은 음수여야 한다"


def test_non_movement_key_resolves_to_nothing() -> None:
    assert resolve("j", step_mm=60, turn_deg=20) is None


def test_holding_a_key_keeps_driving(clock: FakeClock) -> None:
    """**누르고 있으면 OS 가 자동반복으로 같은 키를 계속 준다.**

    콘솔에는 키업 사건이 없으므로 이 반복이 "아직 누르고 있다" 의 유일한 증거다.
    """
    t = make(clock)
    for _ in range(5):
        t.press("up", clock.ms)
        assert last(t.tick(clock.ms))["step"] == 60
        clock.advance(100)


def test_release_falls_back_to_stop(clock: FakeClock) -> None:
    """**입력이 끊기면 정지로 수렴한다.** 마지막 명령을 계속 실행하면 안 된다."""
    t = make(clock, release_ms=300)
    t.press("up", clock.ms)
    assert last(t.tick(clock.ms))["type"] == "MOVE"
    clock.advance(200)
    assert last(t.tick(clock.ms))["type"] == "MOVE", "아직 뗀 것으로 보지 않는다"
    clock.advance(100)
    assert last(t.tick(clock.ms))["type"] == "STOP"
    assert t.holding is None


def test_release_threshold_must_outlast_os_autorepeat_delay(clock: FakeClock) -> None:
    """자동반복 첫 지연(250~500ms)보다 짧게 잡으면 **누른 중에도 정지가 섞인다.**

    이 시험은 그 실패 양상을 고정해 둔다 — 기본값을 함부로 줄이지 않도록.
    """
    t = make(clock, release_ms=200)
    t.press("up", clock.ms)
    clock.advance(400)  # OS 가 첫 반복을 내놓기 전
    assert last(t.tick(clock.ms))["type"] == "STOP"


def test_space_stops_immediately_without_waiting_for_release(clock: FakeClock) -> None:
    t = make(clock)
    t.press("up", clock.ms)
    t.press(" ", clock.ms)
    assert last(t.tick(clock.ms))["type"] == "STOP"


def test_estop_returns_wire_at_once_and_latches_failsafe(clock: FakeClock) -> None:
    """비상정지는 **틱을 기다리지 않고** 그 자리에서 나간다."""
    t = make(clock)
    t.press("up", clock.ms)
    wire = t.press("x", clock.ms)
    assert wire is not None and json.loads(wire)["type"] == "ESTOP"
    assert t._behavior.state == "FAILSAFE"
    assert last(t.tick(clock.ms))["type"] == "STOP"


def test_movement_keys_do_nothing_useful_until_failsafe_is_cleared(clock: FakeClock) -> None:
    """페일세이프 중에는 방향키를 눌러도 **정지가 나간다.**

    로봇도 래치 때문에 `MOVE` 를 차단하지만, 호스트가 보내는 것과 로봇이 하는
    것이 어긋나면 로그만 보고 원인을 찾을 수 없다.
    """
    t = make(clock)
    t.press("x", clock.ms)
    t.press("up", clock.ms)
    assert last(t.tick(clock.ms))["type"] == "STOP"


def test_reset_clears_failsafe_and_returns_wire(clock: FakeClock) -> None:
    t = make(clock)
    t.press("x", clock.ms)
    wire = t.press("r", clock.ms)
    assert wire is not None and json.loads(wire)["type"] == "RESET_SAFE"
    assert t._behavior.state == "IDLE"
    assert t._behavior.commander.intent == HALT


def test_quit_key_requests_exit_and_stops(clock: FakeClock) -> None:
    t = make(clock)
    t.press("up", clock.ms)
    t.press("z", clock.ms)
    assert t.quit_requested
    assert last(t.tick(clock.ms))["type"] == "STOP"


def test_pressing_a_direction_enters_manual(clock: FakeClock) -> None:
    t = make(clock)
    assert t._behavior.state == "IDLE"
    t.press("up", clock.ms)
    assert t._behavior.state == "MANUAL"


def test_keys_are_case_insensitive(clock: FakeClock) -> None:
    t = make(clock)
    t.press("W", clock.ms)
    assert last(t.tick(clock.ms))["step"] == 60


def test_release_ms_must_be_positive(clock: FakeClock) -> None:
    behavior = Behavior(Commander(CommandEncoder(clock=clock)))
    with pytest.raises(ValueError):
        Teleop(behavior, release_ms=0)


def test_the_whole_session_is_accepted_by_the_receiver(clock: FakeClock) -> None:
    """조작 한 세션을 그대로 규약 수신기에 물린다 — 로봇이 보는 것과 같은 판정."""
    t = make(clock)
    decoder = CommandDecoder()
    assert decoder.decode(t.session_line).accepted
    script = ["up", "up", "left", "right", " ", "down", "x", "r", "w"]
    for key in script:
        if urgent := t.press(key, clock.ms):
            assert decoder.decode(urgent).accepted
        for line in t.tick(clock.ms):
            result = decoder.decode(line)
            assert result.accepted, f"거부됨: {result.reason} / {line}"
        clock.advance(100)


def test_first_wire_opens_a_session(clock: FakeClock) -> None:
    """첫 전문은 **`STOP` seq=1** 이어야 로봇이 세션 개시로 인정한다 (PROTOCOL 2절)."""
    session = json.loads(make(clock).session_line)
    assert (session["type"], session["seq"]) == ("STOP", 1)


def test_estop_is_heard_after_an_earlier_host_session(clock: FakeClock) -> None:
    """⚠️ **다른 도구를 쓴 로봇에 teleop 을 켜도 비상정지가 닿아야 한다.**

    세션 개시가 없던 동안 첫 전문은 `STATE` seq=1 이었고, 로봇은 이전 세션의 seq 를
    넘을 때까지 `ESTOP` 까지 전부 폐기했다. 새 수신기로만 시험해서 드러나지 않았다.
    """
    decoder = CommandDecoder()
    earlier = Commander(CommandEncoder(clock=clock), period_ms=100)
    assert decoder.decode(earlier.open_session()).accepted
    for _ in range(50):
        clock.advance(100)
        for line in earlier.tick(clock.ms):
            decoder.decode(line)
    clock.advance(1000)

    t = make(clock)
    assert decoder.decode(t.session_line).accepted
    for line in t.tick(clock.ms):
        assert decoder.decode(line).accepted
    wire = t.press("x", clock.ms)
    assert wire is not None
    assert decoder.decode(wire).accepted, "비상정지가 이전 세션의 seq 에 막혔다"


def test_manual_event_is_idempotent(clock: FakeClock) -> None:
    """매 키 입력마다 `MANUAL_ON` 이 들어가도 상태가 흔들리지 않아야 한다."""
    t = make(clock)
    for _ in range(10):
        t.press("up", clock.ms)
        assert t._behavior.state == "MANUAL"
        clock.advance(50)
    assert t._behavior.fsm.handle(Event.MANUAL_OFF) is True


def test_socket_opens_even_when_the_windows_ioctl_is_missing() -> None:
    """**`SIO_UDP_CONNRESET` 은 파이썬 빌드에 따라 없다.**

    이 개발 PC 에 실제로 없어서 `hasattr` 가드 없이 부르자 도구가 즉사했다.
    소켓 생성은 어떤 환경에서도 예외 없이 끝나야 한다.
    """
    from tools.teleop import open_socket

    sock = open_socket()
    try:
        assert sock.fileno() != -1
    finally:
        sock.close()
