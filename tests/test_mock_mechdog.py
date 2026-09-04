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

import itertools
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


#: 명령마다 새 seq 를 준다. 같은 seq 를 두 번 보내면 규칙 ①로 폐기되어,
#: 테스트가 "명령을 보냈다"고 착각한 채 실제로는 아무것도 전달되지 않는다.
_SEQ = itertools.count(1)


def _encoder(now_ms: int) -> p.CommandEncoder:
    return p.CommandEncoder(clock=lambda: now_ms, start_seq=next(_SEQ))


def _feed(robot: MockRobot, now_ms: int) -> None:
    """유효 명령 한 건을 먹인다. 링크를 살려 두는 것이 목적이다."""
    result = robot.receive(_encoder(now_ms).move(60, 0), now_ms)
    assert result is not None and result.accepted, "명령이 전달되지 않으면 시험이 무의미하다"


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
    assert onboard < set(p.FSM_STATES)

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


# ══════════════════════════════════════════════════════════════
#  STATE — 로봇은 호스트가 알려준 상태를 되돌려준다
# ══════════════════════════════════════════════════════════════


def _send_state(robot: MockRobot, state: str, now_ms: int) -> None:
    result = robot.receive(_encoder(now_ms).state(state), now_ms)
    assert result is not None and result.accepted, "STATE 가 전달되지 않으면 시험이 무의미하다"


def test_host_state_is_echoed_back(config: dict) -> None:
    """호스트가 알려준 상태가 텔레메트리에 그대로 실려야 한다."""
    robot = _robot(config)
    _feed(robot, START_MS)
    _send_state(robot, "ALERT", START_MS)
    assert json.loads(robot.telemetry(START_MS))["state"] == "ALERT"


def test_host_state_survives_subsequent_commands(config: dict) -> None:
    """STATE 는 상태가 바뀔 때만 오고 MOVE 는 10Hz 로 온다.

    MOVE 하나에 상태가 지워지면 로봇 보고가 0.1초마다 PATROL 로 되돌아간다.
    """
    robot = _robot(config)
    _send_state(robot, "TRACK", START_MS)
    for tick in range(1, 20):
        _feed(robot, START_MS + tick * 100)
    assert json.loads(robot.telemetry(START_MS + 2_000))["state"] == "TRACK"


def test_tier1_overrides_host_state(config: dict) -> None:
    """⚠️ **호스트가 뭐라 하든 온보드 안전 판정이 우선한다** (PRD 2.2 불변 규칙).

    이 순서가 뒤집히면 Tier 1 이 Tier 2 에 종속되어 계층 구조의 의미가 사라진다.
    """
    tipped = _robot(config, tip_at_s=0)
    _feed(tipped, START_MS)
    _send_state(tipped, "PATROL", START_MS)
    assert tipped.state(START_MS) == "FAILSAFE", "전도 중에는 호스트 말을 따르지 않는다"

    obstacle = _robot(config, obstacle_at_s=0)
    _feed(obstacle, START_MS)
    _send_state(obstacle, "TRACK", START_MS)
    assert obstacle.state(START_MS) == "AVOID", "반사 정지 중에는 호스트 말을 따르지 않는다"


def test_unknown_host_state_is_ignored_and_previous_one_kept(config: dict) -> None:
    """미지 상태는 폐기하되, 직전에 알던 상태를 잃어버리면 안 된다."""
    robot = _robot(config)
    _send_state(robot, "ALERT", START_MS)
    bogus = p.serialize({"seq": next(_SEQ), "ts": START_MS, "type": "STATE", "state": "DANCING"})
    result = robot.receive(bogus, START_MS)

    assert result is not None
    assert result.verdict is p.Verdict.DISCARD_WARN
    assert json.loads(robot.telemetry(START_MS))["state"] == "ALERT"


def test_defaults_to_patrol_before_any_state_command(config: dict) -> None:
    """STATE 를 못 받았어도 센서와 모순되지 않는 값을 내야 한다."""
    robot = _robot(config)
    _feed(robot, START_MS)
    assert robot.state(START_MS) == "PATROL"


@pytest.mark.parametrize("state", sorted(p.FSM_STATES))
def test_echoed_records_still_pass_host_validation(config: dict, state: str) -> None:
    """8종을 되돌려줘도 호스트 검증을 통과해야 한다.

    특히 규칙 ⑤ — 전도 중에 호스트가 PATROL 을 지시하면, 그대로 실어 보냈다가는
    `tipped:true` + `PATROL` 이 되어 호스트가 자기 레코드를 폐기한다.
    Tier 1 우선 규칙이 이것을 원리적으로 막는다.
    """
    robot = _robot(config, tip_at_s=0)
    _feed(robot, START_MS)
    _send_state(robot, state, START_MS)
    assert p.TelemetryDecoder().decode(robot.telemetry(START_MS)).accepted


def test_posture_survives_following_commands(config: dict) -> None:
    """**자세는 다음 지시가 올 때까지 유지된다.**

    호스트는 자세를 바꿀 때만 `POSE` 를 보내고 `MOVE` 는 10Hz 로 보낸다. 명령
    하나가 피치를 0 으로 되돌리면 뒤따르는 `MOVE` 한 건에 자세가 사라져,
    자세 상승 시퀀스(FR-9.2.2)가 먹혔는지 호스트가 확인할 방법이 없다.
    """
    robot = _robot(config)
    encoder = p.CommandEncoder(clock=lambda: START_MS, start_seq=next(_SEQ) * 1000)
    robot.receive(encoder.pose(15, 0, 0, 300), START_MS)
    assert json.loads(robot.telemetry(START_MS))["imu"]["pitch"] == 15

    for follow_up in (
        encoder.stop(),
        encoder.state("ALERT"),
        encoder.led("red", 2),
        encoder.move(60, 0),
    ):
        robot.receive(follow_up, START_MS)
        pitch = json.loads(robot.telemetry(START_MS))["imu"]["pitch"]
        assert pitch == 15, f"자세가 사라졌다: {follow_up}"


def test_new_pose_replaces_the_previous_one(config: dict) -> None:
    """유지는 하되 새 `POSE` 는 반영해야 한다 — 복귀(FR-9.2.3)가 이 경로다."""
    robot = _robot(config)
    encoder = p.CommandEncoder(clock=lambda: START_MS, start_seq=next(_SEQ) * 1000)
    robot.receive(encoder.pose(15, 0, 0, 300), START_MS)
    robot.receive(encoder.pose(0, 0, 0, 300), START_MS)
    assert json.loads(robot.telemetry(START_MS))["imu"]["pitch"] == 0
