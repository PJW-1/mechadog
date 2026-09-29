"""안전 조건 조합 — 온보드(Tier 1) 판정의 우선순위.

**우선순위는 하나다.**

    E-Stop · 명령 타임아웃 · 링크 두절 · 저전압  >  근거리 정지  >  호스트 명령

앞의 넷은 **안전 래치**로 들어오고, 근거리 정지는 그 아래에서 **전진만** 막는다.
호스트가 `PATROL` 이라고 알려줘도 로봇이 위험을 보면 `FAILSAFE` 를 보고한다 —
Tier 1 이 Tier 2 에 굴복하면 이 구조의 의미가 사라진다(아키텍처 1.2).

**여기가 그 규칙의 단일 출처다.** 같은 규칙이 세 곳에 있다.

| 어디 | 무엇 |
| :--- | :--- |
| 펌웨어 | `firmware_mechdog_motion/src/safety_monitor.h` 의 `move_allowed()`·`reset_safe_allowed()` |
| 그 시험 | `firmware_mechdog_motion/test/test_safety_monitor.cpp` (PC 에서 전수) |
| 가상 로봇 | `tools/mock_mechdog.py` — 호스트 시험 전부가 이것을 상대로 돈다 |

⚠️ **가상 로봇이 펌웨어보다 엄격하면 호스트 시험은 통과하는데 실물은 다르게 동작한다.**
예컨대 가상 로봇이 저전압 중 `RESET_SAFE` 를 거부하는데 펌웨어는 받아 준다면, 그 어긋남을
이 파일이 놓치지 않고 잡는다. 양쪽이 같은 규칙을 따르는지 보는 자리다.
"""

import itertools
import json

import pytest

from host.common import protocol as p
from tools.mock_mechdog import Faults, MockRobot, load_config

START_MS = 1_756_800_000_000

#: 명령마다 새 seq 를 준다. 같은 seq 를 두 번 보내면 규칙 ①로 폐기되어,
#: 시험이 "명령을 보냈다" 고 착각한 채 실제로는 아무것도 전달되지 않는다.
_SEQ = itertools.count(1)


@pytest.fixture
def config() -> dict:
    """가상 로봇과 같은 설정을 쓴다 — 임계를 코드에 박으면 진짜 로봇과 다른
    기준으로 호스트를 시험하게 된다."""
    return load_config()


def _robot(config: dict, **faults: object) -> MockRobot:
    return MockRobot(
        device_id="mechdog-mock",
        cfg=config,
        faults=Faults(**faults),  # type: ignore[arg-type]
        start_ms=START_MS,
    )


def _armed(config: dict, **faults: object) -> MockRobot:
    """래치를 푼 로봇. 펌웨어처럼 래치된 채 부팅하므로 사람이 `RESET_SAFE` 를 보낸 뒤다."""
    robot = _robot(config, **faults)
    _send(robot, START_MS, _encoder(START_MS).reset_safe())
    return robot


def _encoder(now_ms: int) -> p.CommandEncoder:
    return p.CommandEncoder(clock=lambda: now_ms, start_seq=next(_SEQ))


def _send(robot: MockRobot, now_ms: int, packet: str) -> None:
    result = robot.receive(packet, now_ms)
    assert result is not None and result.accepted, "명령이 전달되지 않으면 시험이 무의미하다"


def _flags(robot: MockRobot, now_ms: int) -> dict:
    line = robot.telemetry(now_ms)
    assert line is not None
    return json.loads(line)


# ══════════════════════════════════════════════
#  래치를 만드는 넷 — 전부 호스트 명령을 이긴다
# ══════════════════════════════════════════════


def test_estop_beats_every_other_condition(config: dict) -> None:
    """E-Stop 은 어떤 상태에서도 최우선이다 (NFR-2.7)."""
    robot = _robot(config, obstacle_at_s=0)
    now = START_MS
    _send(robot, now, _encoder(now).estop())
    assert robot.state(now) == "FAILSAFE", "장애물 상태여도 E-Stop 이 이긴다"

    now += 100
    _send(robot, now, _encoder(now).move(-60, 0))
    assert robot.state(now) == "FAILSAFE", "래치 중에는 후진도 나가지 않는다"
    assert robot.motion(now) == {"step": 0.0, "angle": 0.0}


def test_command_timeout_latches_failsafe(config: dict) -> None:
    """600ms 무명령이면 곧바로 래치다 — 펌웨어 `latchFailsafe("command timeout")` (ADR-39)."""
    timeout = config["safety"]["cmd_timeout_ms"]
    robot = _armed(config)
    now = START_MS
    _send(robot, now, _encoder(now).move(60, 0))
    assert robot.motion(now)["step"] > 0

    assert robot.state(now + timeout - 1) != "FAILSAFE"
    quiet = now + timeout
    assert robot.state(quiet) == "FAILSAFE"
    assert robot.motion(quiet) == {"step": 0.0, "angle": 0.0}
    assert _flags(robot, quiet)["safety_latched"] is True


def test_boots_latched_and_refuses_move_until_reset_safe(config: dict) -> None:
    """펌웨어는 래치된 채 부팅한다 (`motion_safety_state.h` `safe_latched = true`).

    세션 개시 `STOP` 도, 링크가 살아났다는 사실도 래치를 풀지 않는다 — 받아들인
    `RESET_SAFE` 만 푼다. 가짜가 실물보다 친절하면 호스트가 실기에서만 막힌다.
    """
    robot = _robot(config)
    now = START_MS
    assert _flags(robot, now)["safety_latched"] is True

    _send(robot, now, _encoder(now).stop())
    _send(robot, now, _encoder(now).move(60, 0))
    record = _flags(robot, now)
    assert record["state"] == "FAILSAFE"
    assert record["safety_latched"] is True
    assert record["flags"]["link_ok"] is True, "링크는 살아 있고 래치만 걸려 있다"
    assert robot.motion(now) == {"step": 0.0, "angle": 0.0}, "해제 전 MOVE 는 거부된다"

    now += 100
    _send(robot, now, _encoder(now).reset_safe())
    record = _flags(robot, now)
    assert record["safety_latched"] is False
    assert record["state"] == "IDLE", "해제는 보고 상태를 IDLE 로 되돌린다 (펌웨어와 같다)"

    now += 100
    _send(robot, now, _encoder(now).move(60, 0))
    assert robot.motion(now)["step"] > 0


def test_timeout_latch_refuses_move_until_reset_safe(config: dict) -> None:
    """명령이 다시 와도 래치는 `RESET_SAFE` 를 기다린다. 풀린 뒤에는 새 `MOVE` 부터 걷는다."""
    timeout = config["safety"]["cmd_timeout_ms"]
    robot = _robot(config)
    now = START_MS
    _send(robot, now, _encoder(now).move(60, 0))

    now += timeout + 10
    _send(robot, now, _encoder(now).move(60, 0))
    assert robot.state(now) == "FAILSAFE"
    assert robot.motion(now) == {"step": 0.0, "angle": 0.0}, "래치 중 MOVE 는 거부된다"

    now += 100
    _send(robot, now, _encoder(now).reset_safe())
    assert _flags(robot, now)["safety_latched"] is False
    assert robot.motion(now) == {"step": 0.0, "angle": 0.0}, "해제만으로 걷기를 재개하지 않는다"

    now += 100
    _send(robot, now, _encoder(now).move(60, 0))
    assert robot.state(now) != "FAILSAFE"
    assert robot.motion(now)["step"] > 0


def test_low_battery_latches_and_beats_the_host_view(config: dict) -> None:
    """호스트가 `PATROL` 이라고 알려줘도 로봇은 `FAILSAFE` 를 보고한다."""
    shutdown = config["safety"]["battery_shutdown_v"]
    robot = _robot(config, battery_start_v=shutdown - 0.1)
    now = START_MS
    _send(robot, now, _encoder(now).state("PATROL"))
    assert robot.state(now) == "FAILSAFE"
    assert _flags(robot, now)["flags"]["lowbatt"] is True


# ══════════════════════════════════════════════
#  근거리 정지 — 래치보다 아래, 호스트 명령보다 위
# ══════════════════════════════════════════════


def test_obstacle_blocks_forward_only(config: dict) -> None:
    """⚠️ 전부 막으면 `FR-2.3` 의 «정지 후 후진» 이 실행 불가가 된다."""
    robot = _armed(config, obstacle_at_s=0)
    now = START_MS
    _send(robot, now, _encoder(now).move(60, 0))
    assert robot.motion(now) == {"step": 0.0, "angle": 0.0}, "전진은 막힌다"

    now += 100
    _send(robot, now, _encoder(now).move(-60, 0))
    assert robot.motion(now)["step"] < 0, "후진은 나가야 빠져나갈 수 있다"


def test_obstacle_is_reported_but_does_not_latch(config: dict) -> None:
    """반사 정지는 래치가 아니다 — 사람 확인 없이 스스로 풀려야 한다 (ADR-22)."""
    robot = _armed(config, obstacle_at_s=0)
    now = START_MS
    _send(robot, now, _encoder(now).move(60, 0))
    record = _flags(robot, now)
    assert record["state"] == "AVOID"
    assert record["flags"]["obstacle"] is True
    assert record["safety_latched"] is False


def test_latch_wins_over_obstacle_in_the_reported_state(config: dict) -> None:
    """둘 다 성립하면 래치가 보고를 가져간다 — 더 위험한 쪽을 말한다."""
    robot = _robot(config, obstacle_at_s=0)
    now = START_MS
    _send(robot, now, _encoder(now).estop())
    record = _flags(robot, now)
    assert record["state"] == "FAILSAFE", "장애물보다 래치가 먼저다"
    assert record["flags"]["obstacle"] is True, "그래도 장애물 사실은 계속 보고한다"


# ══════════════════════════════════════════════
#  해제 — 원인이 남아 있으면 풀 수 없다
# ══════════════════════════════════════════════


def test_reset_safe_is_refused_while_the_cause_remains(config: dict) -> None:
    """⚠️ 풀어 주면 다음 `MOVE` 에서 또 걸린다 — 사람은 해제만 반복하게 된다.

    원인이 남아 있는데 펌웨어가 `RESET_SAFE` 를 받아 주면 이 반복이 실물에서도 일어난다.
    """
    shutdown = config["safety"]["battery_shutdown_v"]
    robot = _robot(config, battery_start_v=shutdown - 0.1)
    now = START_MS
    _send(robot, now, _encoder(now).move(60, 0))
    assert robot.state(now) == "FAILSAFE"

    now += 100
    _send(robot, now, _encoder(now).reset_safe())
    assert robot.state(now) == "FAILSAFE", "저전압이 남아 있는데 래치가 풀렸다"


def test_reset_safe_works_once_the_cause_is_gone(config: dict) -> None:
    """원인이 없으면 풀린다 — 거부가 «영영 못 푼다» 가 되면 안 된다."""
    shutdown = config["safety"]["battery_shutdown_v"]
    robot = _robot(config, battery_start_v=shutdown + 0.5)
    now = START_MS
    _send(robot, now, _encoder(now).estop())
    assert robot.state(now) == "FAILSAFE"

    now += 100
    _send(robot, now, _encoder(now).reset_safe())
    now += 100
    _send(robot, now, _encoder(now).move(60, 0))
    assert robot.state(now) != "FAILSAFE"
    assert robot.motion(now)["step"] > 0


def test_obstacle_does_not_block_the_reset(config: dict) -> None:
    """근거리 정지는 래치 원인이 아니므로 해제를 막지 않는다."""
    robot = _robot(config, obstacle_at_s=0)
    now = START_MS
    _send(robot, now, _encoder(now).estop())
    assert robot.state(now) == "FAILSAFE"

    now += 100
    _send(robot, now, _encoder(now).reset_safe())
    now += 100
    _send(robot, now, _encoder(now).move(-60, 0))
    assert robot.state(now) == "AVOID", "래치는 풀리고 장애물 보고만 남는다"
    assert robot.motion(now)["step"] < 0
