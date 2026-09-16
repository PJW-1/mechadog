"""안전 조건 조합 — 온보드(Tier 1) 판정의 우선순위 (WBS 6.2.1 ③ · 3.2.5).

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
실제로 2026-09-17 까지 `RESET_SAFE` 가 그랬다 — 가상 로봇은 저전압 중 거부했고 펌웨어는
받아 줬다. 이 파일은 양쪽이 같은 규칙을 따르는지 보는 자리다.
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


def test_command_timeout_stops_the_legs_without_a_state_transition(config: dict) -> None:
    """300ms 무명령 → `move(0,0)`. 상태 전이가 아니라 Tier 1 반사다."""
    timeout = config["safety"]["cmd_timeout_ms"]
    robot = _robot(config)
    now = START_MS
    _send(robot, now, _encoder(now).move(60, 0))
    assert robot.motion(now)["step"] > 0

    quiet = now + timeout
    assert robot.stopped_by_timeout(quiet)
    assert robot.motion(quiet) == {"step": 0.0, "angle": 0.0}


def test_link_loss_latches_failsafe(config: dict) -> None:
    """3초 두절이면 래치다 — 타임아웃 정지보다 한 단계 위다 (FR-1.5)."""
    link_loss = config["safety"]["link_loss_failsafe_ms"]
    robot = _robot(config)
    now = START_MS
    _send(robot, now, _encoder(now).move(60, 0))
    assert robot.state(now) != "FAILSAFE"

    gone = now + link_loss + 1
    assert robot.state(gone) == "FAILSAFE"
    # 명령이 다시 와도 래치는 사람의 해제를 기다린다.
    _send(robot, gone + 10, _encoder(gone + 10).move(60, 0))
    assert robot.state(gone + 20) == "FAILSAFE"


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
    robot = _robot(config, obstacle_at_s=0)
    now = START_MS
    _send(robot, now, _encoder(now).move(60, 0))
    assert robot.motion(now) == {"step": 0.0, "angle": 0.0}, "전진은 막힌다"

    now += 100
    _send(robot, now, _encoder(now).move(-60, 0))
    assert robot.motion(now)["step"] < 0, "후진은 나가야 빠져나갈 수 있다"


def test_obstacle_is_reported_but_does_not_latch(config: dict) -> None:
    """반사 정지는 래치가 아니다 — 사람 확인 없이 스스로 풀려야 한다 (ADR-22)."""
    robot = _robot(config, obstacle_at_s=0)
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

    펌웨어는 2026-09-17 까지 이것을 받아 줬다(`3.2.5` 에서 맞췄다).
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
