"""통신·설정·안전 복구의 실제 실패 사례 회귀 검증."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from host.common.config import ConfigError, load_base_config, load_config, validate_base_config
from host.common.protocol import CommandDecoder, CommandEncoder, TelemetryDecoder, TelemetryEncoder
from tools.mock_mechdog import Faults, MockRobot


def test_huge_command_number_is_discarded_and_next_packet_survives():
    decoder = CommandDecoder()
    raw = json.dumps({"seq": 1, "ts": 1, "type": "MOVE", "step": 10**400, "angle": 0})
    assert len(raw) < 2048
    assert not decoder.decode(raw).accepted
    assert decoder.decode('{"seq":2,"ts":2,"type":"STOP"}').accepted


def test_huge_telemetry_number_is_discarded():
    record = TelemetryEncoder("unit").build(
        "PATROL",
        50,
        {"pitch": 0, "roll": 0, "yaw": 0},
        8.0,
        0,
        {"lowbatt": False, "tipped": False, "link_ok": True},
    )
    record["imu"]["pitch"] = 10**400
    assert not TelemetryDecoder().decode(json.dumps(record)).accepted


def test_shared_session_fixture():
    decoder = CommandDecoder()
    path = Path(__file__).parent / "fixtures" / "protocol_sessions.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        assert decoder.decode(line).verdict == case["_expect"], case


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("safety", "battery_shutdown_v", 1),
        ("safety", "tip_angle_deg", 999),
        ("safety", "tip_angle_deg", 10**400),
        ("network", "cmd_rate_hz", 1),
        ("vision", "ppe", {}),
        ("localization", "track", "phone_vio"),
    ],
)
def test_invalid_config_is_rejected(section, key, value):
    cfg = load_base_config()
    cfg[section][key] = value
    with pytest.raises(ConfigError):
        validate_base_config(cfg)


def test_invalid_device_override_is_rejected(tmp_path):
    root = Path(__file__).resolve().parents[1]
    example = root / "config/devices/ref.yaml.example"
    (tmp_path / "ref.yaml").write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    override = {"network": {"cmd_rate_hz": 1}}
    (tmp_path / "ref.local.yaml").write_text(yaml.safe_dump(override), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config("ref", devices_dir=tmp_path)


@pytest.mark.parametrize("observe_before_reconnect", [True, False])
def test_link_recovery_requires_explicit_reset(observe_before_reconnect):
    robot = MockRobot("unit", load_base_config())
    encoder = CommandEncoder(clock=lambda: 10000)
    robot.receive(encoder.move(60, 20), 0)
    if observe_before_reconnect:
        assert robot.state(3100) == "FAILSAFE"
    robot.receive(encoder.move(60, 20), 3101)
    assert robot.state(3101) == "FAILSAFE"
    assert robot.motion(3101) == {"step": 0, "angle": 0}
    robot.receive(encoder.stop(), 3102)
    assert robot.state(3102) == "FAILSAFE"
    robot.receive(encoder.reset_safe(), 3103)
    assert robot.state(3103) == "IDLE"
    assert robot.motion(3103) == {"step": 0, "angle": 0}


def test_estop_ignores_move_and_state_until_reset():
    robot = MockRobot("unit", load_base_config())
    encoder = CommandEncoder(clock=lambda: 10000)
    for message in (encoder.estop(), encoder.move(60, 0), encoder.state("PATROL")):
        robot.receive(message, 0)
    record = json.loads(robot.telemetry(0))
    assert record["state"] == "FAILSAFE"
    assert record["safety_latched"] is True
    assert record["motion"]["step"] == 0
    robot.receive(encoder.reset_safe(), 1)
    assert robot.state(1) == "IDLE"


@pytest.mark.parametrize("faults", [Faults(battery_start_v=6.6), Faults(tip_at_s=0)])
def test_reset_cannot_clear_active_physical_fault(faults):
    robot = MockRobot("unit", load_base_config(), faults=faults)
    encoder = CommandEncoder(clock=lambda: 10000)
    robot.receive(encoder.reset_safe(), 0)
    assert robot.state(0) == "FAILSAFE"


def test_timeout_does_not_resume_old_move_when_led_heartbeat_returns():
    robot = MockRobot("unit", deepcopy(load_base_config()))
    encoder = CommandEncoder(clock=lambda: 10000)
    robot.receive(encoder.move(60, 20), 0)
    robot.receive(encoder.led("red", 0), 301)
    assert robot.motion(301) == {"step": 0, "angle": 0}
