"""가상 MechDog 검증 (WBS 6.1.1).

목업의 값어치는 **호스트를 진짜와 같은 기준으로 시험한다**는 데 있다. 그래서
가장 중요한 검사는 기능 목록이 아니라 아래 둘이다.

  · 목업이 내보내는 모든 텔레메트리가 `TelemetryDecoder` 를 통과하는가
    — 규약을 어긴 레코드를 내보내면 호스트가 폐기해 버려서, 시험한 줄 알았던
      시나리오가 실은 한 번도 실행되지 않는다
  · 임계값을 `config.yaml` 에서 읽는가
    — 목업이 자체 숫자를 갖는 순간 진짜 로봇과 다른 기준이 된다

시각을 주입하므로 30초 뒤의 전도도, 10분간의 방전도 여기서 즉시 검증된다.
"""

import json

import pytest
from conftest import FakeClock

from host.common import protocol as p
from tools.mock_mechdog import Faults, MockRobot, _describe, build_parser, load_config

START_MS = 1_756_800_000_000


@pytest.fixture(scope="module")
def config() -> dict:
    return load_config()


def _robot(config: dict, **faults: object) -> MockRobot:
    return MockRobot(
        device_id="mechdog-mock",
        cfg=config,
        faults=Faults(**faults),  # type: ignore[arg-type]
        start_ms=START_MS,
    )


def _feed(robot: MockRobot, now_ms: int, encoder: p.CommandEncoder | None = None) -> None:
    """유효 명령 한 건을 먹인다. 링크를 살려 두는 것이 목적이다."""
    encoder = encoder or p.CommandEncoder(clock=lambda: now_ms)
    robot.receive(encoder.move(60, 0), now_ms)


# ══════════════════════════════════════════════════════════════
#  규약 준수 — 이것이 깨지면 나머지 시험이 전부 무의미해진다
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "faults",
    [
        {},
        {"tip_at_s": 0},
        {"obstacle_at_s": 0},
        {"battery_drain_v_per_min": 6.0},
        {"battery_start_v": 6.6},
    ],
    ids=["정상", "전도", "장애물", "방전", "셧다운경계"],
)
def test_every_emitted_record_passes_host_validation(config: dict, faults: dict) -> None:
    """어떤 고장 상태에서도 호스트가 받아들일 수 있는 레코드여야 한다."""
    robot = _robot(config, **faults)
    decoder = p.TelemetryDecoder()
    for tick in range(30):
        now = START_MS + tick * 100
        _feed(robot, now)
        line = robot.telemetry(now)
        assert line is not None
        result = decoder.decode(line)
        assert result.accepted, f"tick {tick}: {result.reason}"


def test_thresholds_come_from_config(config: dict) -> None:
    """임계값을 코드에 박으면 진짜 로봇과 다른 기준으로 호스트를 시험하게 된다."""
    shutdown = config["safety"]["battery_shutdown_v"]
    robot = _robot(config, battery_start_v=shutdown)
    _feed(robot, START_MS)
    assert robot.state(START_MS) == "FAILSAFE"

    above = _robot(config, battery_start_v=shutdown + 0.1)
    _feed(above, START_MS)
    assert above.state(START_MS) == "PATROL"


# ══════════════════════════════════════════════════════════════
#  링크 — 호스트가 송신을 멈추면 무슨 일이 일어나는가
# ══════════════════════════════════════════════════════════════


def test_starts_in_failsafe_until_first_command(config: dict) -> None:
    """한 번도 명령을 받지 못한 로봇은 링크가 살아 있다고 보고하면 안 된다."""
    robot = _robot(config)
    assert robot.state(START_MS) == "FAILSAFE"
    assert not robot.link_ok(START_MS)


def test_command_timeout_stops_but_does_not_change_state(config: dict) -> None:
    """300ms 무명령은 Tier 1 반사(`move(0,0)`)이지 상태 전이가 아니다 (PRD 5절)."""
    robot = _robot(config)
    _feed(robot, START_MS)
    timeout_ms = config["safety"]["cmd_timeout_ms"]

    assert not robot.stopped_by_timeout(START_MS + timeout_ms)
    assert robot.stopped_by_timeout(START_MS + timeout_ms + 1)
    assert robot.state(START_MS + timeout_ms + 1) == "PATROL"


def test_link_loss_enters_failsafe(config: dict) -> None:
    robot = _robot(config)
    _feed(robot, START_MS)
    failsafe_ms = config["safety"]["link_loss_failsafe_ms"]

    assert robot.state(START_MS + failsafe_ms) == "PATROL"
    assert robot.state(START_MS + failsafe_ms + 1) == "FAILSAFE"

    record = json.loads(robot.telemetry(START_MS + failsafe_ms + 1))
    assert record["flags"]["link_ok"] is False
    assert record["last_cmd_age_ms"] == failsafe_ms + 1


def test_discarded_command_does_not_refresh_the_link(config: dict) -> None:
    """규칙 ③ — 깨진 패킷을 "살아 있음"으로 세면 페일세이프가 걸리지 않는다."""
    robot = _robot(config)
    _feed(robot, START_MS)
    later = START_MS + config["safety"]["link_loss_failsafe_ms"] + 1

    robot.receive("{망가진 패킷", later)
    assert robot.state(later) == "FAILSAFE"


# ══════════════════════════════════════════════════════════════
#  장애 주입
# ══════════════════════════════════════════════════════════════


def test_packet_loss_is_reproducible_with_a_seed(config: dict) -> None:
    """같은 씨앗이면 같은 패킷이 사라진다. 재현되지 않는 고장은 디버깅할 수 없다."""

    def run() -> list[bool]:
        robot = _robot(config, drop_rate=0.5, seed=42)
        encoder = p.CommandEncoder(clock=lambda: START_MS)
        return [robot.receive(encoder.stop(), START_MS) is None for _ in range(20)]

    first, second = run(), run()
    assert first == second
    assert any(first) and not all(first), "20건 중 일부만 사라져야 의미가 있다"


def test_dropped_packets_never_reach_the_decoder(config: dict) -> None:
    """유실은 파싱 실패와 다르다 — 도착조차 하지 않은 것이므로 수신 통계 밖이다."""
    robot = _robot(config, drop_rate=1.0)
    encoder = p.CommandEncoder(clock=lambda: START_MS)
    for _ in range(10):
        assert robot.receive(encoder.stop(), START_MS) is None
    assert robot.stats.dropped == 10
    assert robot.stats.received == 0
    assert robot.state(START_MS) == "FAILSAFE", "전부 유실되면 링크가 죽은 것과 같다"


def test_battery_drain_crosses_warn_then_shutdown(config: dict) -> None:
    """실물로는 수십 분 걸리는 방전을 여기서는 즉시 통과시킨다."""
    warn = config["safety"]["battery_warn_v"]
    shutdown = config["safety"]["battery_shutdown_v"]
    robot = _robot(config, battery_drain_v_per_min=1.0)

    def at(minutes: float) -> dict:
        now = START_MS + int(minutes * 60_000)
        _feed(robot, now)
        return json.loads(robot.telemetry(now))

    assert at(0)["flags"]["lowbatt"] is False
    assert at(p.BATT_MAX_V - warn)["flags"]["lowbatt"] is True
    assert at(p.BATT_MAX_V - shutdown)["state"] == "FAILSAFE"


def test_battery_never_drops_below_physical_floor(config: dict) -> None:
    """6.0V 아래로 가면 호스트가 레코드째 폐기해 셧다운이 화면에 안 나타난다."""
    robot = _robot(config, battery_drain_v_per_min=60.0)
    late = START_MS + 600_000
    _feed(robot, late)
    assert robot.battery_v(late) == p.BATT_MIN_V
    assert p.TelemetryDecoder().decode(robot.telemetry(late)).accepted


def test_tip_sets_flag_and_failsafe_together(config: dict) -> None:
    """`tipped` 인데 `PATROL` 이면 호스트가 규칙 ⑤로 폐기한다. 둘이 같이 가야 한다."""
    robot = _robot(config, tip_at_s=30)
    before, after = START_MS + 29_000, START_MS + 30_000
    _feed(robot, before)
    _feed(robot, after)

    assert json.loads(robot.telemetry(before))["flags"]["tipped"] is False
    record = json.loads(robot.telemetry(after))
    assert record["flags"]["tipped"] is True
    assert record["state"] == "FAILSAFE"
    assert record["imu"]["pitch"] > config["safety"]["tip_angle_deg"]


def test_obstacle_triggers_avoid_below_config_threshold(config: dict) -> None:
    robot = _robot(config, obstacle_at_s=10)
    before, after = START_MS + 9_000, START_MS + 10_000
    _feed(robot, before)
    _feed(robot, after)

    assert robot.state(before) == "PATROL"
    assert robot.state(after) == "AVOID"
    assert robot.distance_cm(after) < config["safety"]["obstacle_stop_cm"]


def test_go_silent_stops_telemetry(config: dict) -> None:
    """침묵은 로봇이 사라진 것과 같다. 호스트의 수신 타임아웃을 시험한다."""
    robot = _robot(config, go_silent_at_s=5)
    _feed(robot, START_MS)
    assert robot.telemetry(START_MS + 4_999) is not None
    assert robot.telemetry(START_MS + 5_000) is None


def test_corrupt_telemetry_is_rejected_by_the_host(config: dict) -> None:
    """호스트 규칙 ③이 실제로 걸리는지 확인한다."""
    robot = _robot(config, corrupt_rate=1.0)
    _feed(robot, START_MS)
    result = p.TelemetryDecoder().decode(robot.telemetry(START_MS))
    assert result.verdict is p.Verdict.DISCARD
    assert not result.refreshes_link


# ══════════════════════════════════════════════════════════════
#  텔레메트리 내용
# ══════════════════════════════════════════════════════════════


def test_reports_only_states_the_robot_can_know(config: dict) -> None:
    """FSM 은 호스트에 있다 (PRD 5절). 로봇이 낼 수 있는 상태는 셋뿐이다.

    나머지 5종을 실으려면 호스트가 상태를 내려보내야 한다 — 규약의 열린 구멍.
    """
    onboard = {"PATROL", "AVOID", "FAILSAFE"}
    assert onboard < set(p.TELEMETRY_STATES)

    seen = set()
    for faults in ({}, {"obstacle_at_s": 0}, {"tip_at_s": 0}):
        robot = _robot(config, **faults)
        _feed(robot, START_MS)
        seen.add(robot.state(START_MS))
    assert seen == onboard


def test_pose_command_is_reflected_in_imu_pitch(config: dict) -> None:
    """자세 명령이 IMU 에 나타나야 자세 상승 시퀀스(FR-9.2.2)를 시험할 수 있다."""
    robot = _robot(config)
    encoder = p.CommandEncoder(clock=lambda: START_MS)
    robot.receive(encoder.pose(15, 0, 0, 300), START_MS)
    assert json.loads(robot.telemetry(START_MS))["imu"]["pitch"] == 15


def test_clamped_command_is_counted_separately(config: dict) -> None:
    """클램핑은 수락이지만 조용히 넘어가면 안 된다 — 송신측 버그의 신호다."""
    robot = _robot(config)
    result = robot.receive('{"seq":1,"ts":1,"type":"MOVE","step":500,"angle":0}', START_MS)
    assert result is not None
    assert result.verdict is p.Verdict.CLAMP
    assert robot.stats.clamped == 1
    assert robot.stats.accepted == 1


def test_telemetry_seq_is_monotonic(config: dict) -> None:
    robot = _robot(config)
    _feed(robot, START_MS)
    seqs = [json.loads(robot.telemetry(START_MS + i * 100))["seq"] for i in range(5)]
    assert seqs == [1, 2, 3, 4, 5]


def test_telemetry_timestamp_follows_the_injected_clock(config: dict) -> None:
    """`ts` 가 실제 시계를 보면 목업의 시나리오를 앞당길 수 없다."""
    robot = _robot(config)
    _feed(robot, START_MS)
    record = json.loads(robot.telemetry(START_MS + 12_345))
    assert record["ts"] == START_MS + 12_345


def test_fake_clock_matches_the_mock_time_base() -> None:
    """conftest 의 가짜 시계와 목업의 기준 시각이 같아야 한다."""
    assert FakeClock()() == START_MS


# ══════════════════════════════════════════════════════════════
#  CLI 배선 — 소켓 루프는 시험 대상이 아니지만 옵션 연결은 맞다
# ══════════════════════════════════════════════════════════════


def test_cli_options_map_onto_faults() -> None:
    """옵션 이름이 바뀌면 시나리오 재현 스크립트가 조용히 무력화된다."""
    args = build_parser().parse_args(
        [
            "--device",
            "mechdog-b",
            "--drop-rate",
            "0.25",
            "--corrupt-rate",
            "0.1",
            "--battery-start",
            "7.4",
            "--battery-drain",
            "0.5",
            "--tip-at",
            "30",
            "--obstacle-at",
            "15",
            "--go-silent",
            "20",
            "--seed",
            "42",
        ]
    )
    assert args.device == "mechdog-b"
    faults = Faults(
        drop_rate=args.drop_rate,
        corrupt_rate=args.corrupt_rate,
        battery_start_v=args.battery_start,
        battery_drain_v_per_min=args.battery_drain,
        tip_at_s=args.tip_at,
        obstacle_at_s=args.obstacle_at,
        go_silent_at_s=args.go_silent,
        seed=args.seed,
    )
    assert faults == Faults(0.25, 0.1, 7.4, 0.5, 30.0, 15.0, 20.0, 42)


def test_cli_defaults_are_a_healthy_robot() -> None:
    """옵션 없이 띄우면 고장 없는 로봇이어야 한다."""
    args = build_parser().parse_args([])
    assert (args.drop_rate, args.corrupt_rate, args.battery_drain) == (0.0, 0.0, 0.0)
    assert (args.tip_at, args.obstacle_at, args.go_silent, args.seed) == (None,) * 4


def test_log_line_distinguishes_loss_discard_and_clamp(config: dict) -> None:
    """로그만 보고 유실·폐기·클램핑이 구분되어야 한다. 셋은 원인이 전혀 다르다."""
    robot = _robot(config)
    accepted = robot.receive('{"seq":1,"ts":1,"type":"MOVE","step":60,"angle":0}', START_MS)
    clamped = robot.receive('{"seq":2,"ts":1,"type":"MOVE","step":500,"angle":0}', START_MS)
    discarded = robot.receive("{깨진 패킷", START_MS)

    assert _describe(accepted) == "MOVE step=60 angle=0"
    assert "클램핑 step" in _describe(clamped)
    assert _describe(discarded).startswith("폐기(")
    assert _describe(None) == "유실"
