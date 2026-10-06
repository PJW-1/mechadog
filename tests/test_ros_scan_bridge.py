"""One rotation assembled from the real UDP decoder's sector format."""

import math
import runpy
from pathlib import Path

from host.common.lidar_link import ScanDecoder, encode_scan, scan_of

_BRIDGE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "docker/ros2/scan_bridge.py"))
Revolution = _BRIDGE["Revolution"]
mount_from_env = _BRIDGE["mount_from_env"]


def test_sectors_become_fixed_bins_with_missing_beams() -> None:
    decoder = ScanDecoder()
    rotation = Revolution(4, 0.12, 8.0)
    sectors = (
        [[0.0, 1000], [90.0, 2000]],
        [[270.0, 3000]],
        [[0.0, 4000]],
    )
    completed = []
    for seq, points in enumerate(sectors, 1):
        raw = encode_scan(
            seq=seq,
            ts_ms=seq * 100,
            device_id="lidar-test",
            boot_id="a" * 16,
            points_wire=points,
        )
        scan = scan_of(decoder.decode(raw))
        assert scan is not None
        completed.extend(rotation.add(scan.points))
    assert completed == [[1.0, 2.0, math.inf, 3.0]]
    assert rotation.flush() == [4.0, math.inf, math.inf, math.inf]


def test_reversed_sensor_bearings_complete_revolution_in_robot_frame() -> None:
    """실측 장착의 감소각에서도 한 바퀴 경계를 찾아 4방위로 발행한다."""
    decoder = ScanDecoder(270, -1)
    rotation = Revolution(4, 0.12, 8.0, -1)
    raw = encode_scan(
        seq=1,
        ts_ms=100,
        device_id="lidar-test",
        boot_id="a" * 16,
        points_wire=[
            [260, 1000],
            [270, 2000],
            [271, 3000],
            [350, 4000],
            [0, 5000],
            [90, 6000],
            [180, 7000],
            [270, 8000],
            [271, 1500],
        ],
    )
    scan = scan_of(decoder.decode(raw))
    assert scan is not None
    completed = rotation.add(scan.points)
    assert completed[-1] == [8.0, 7.0, 6.0, 3.0]


def test_relay_reboot_restarts_seq_without_losing_scans() -> None:
    """중계 노드가 재부팅해 `seq` 가 1 로 돌아가도 스캔이 버려지지 않아야 한다 (WBS 5.4.2).

    ⚠️ **이게 조용히 죽는 경로다.** 순서 게이트(`_SeqGate.admit`)는 `seq <= last`
    를 역전으로 보고 버린다. 재부팅하면 `seq` 가 1 부터 다시 시작하므로, 게이트가
    송신자를 `device_id` 만으로 셌다면 **재부팅 뒤 모든 스캔이 영구히 버려진다** —
    링크는 살아 있고 패킷은 도착하는데 `/scan` 만 조용해진다.

    실제로는 `(device_id, boot_id)` 로 세고 펌웨어가 부팅마다 `boot_id` 를 새로
    만들기 때문에(`firmware/lidar_relay/lidar_relay.ino` 의 `makeBootId` 가
    `esp_random()` 두 번) 새 세션이 열려 통과한다. 그 두 가지가 **함께** 성립해야
    하므로 여기서 함께 확인한다.
    """
    decoder = ScanDecoder()
    before, after = "1" * 16, "2" * 16
    wire = [[0.0, 1000], [90.0, 1200]]

    # 재부팅 전 — seq 가 한참 올라가 있다.
    for seq in (4998, 4999, 5000):
        result = decoder.decode(
            encode_scan(
                seq=seq, ts_ms=seq * 100, device_id="lidar-01", boot_id=before, points_wire=wire
            )
        )
        assert result.accepted, result.reason
    assert decoder.last_seq("lidar-01", before) == 5000

    # 재부팅 — 같은 기기, 새 boot_id, seq 는 1 부터.
    for seq in (1, 2, 3):
        result = decoder.decode(
            encode_scan(
                seq=seq, ts_ms=seq * 100, device_id="lidar-01", boot_id=after, points_wire=wire
            )
        )
        assert result.accepted, f"재부팅 후 seq={seq} 가 버려졌다: {result.reason}"
        assert scan_of(result) is not None

    # 옛 세션의 값은 그대로 남아 늦게 도착한 재부팅 전 패킷도 계속 걸러낸다.
    assert decoder.last_seq("lidar-01", before) == 5000
    assert decoder.last_seq("lidar-01", after) == 3


def test_same_boot_id_after_restart_is_still_rejected() -> None:
    """`boot_id` 가 그대로면 게이트는 여전히 막는다 — 펌웨어가 지켜야 할 약속이다.

    위 시험이 통과하는 이유가 «게이트가 관대해서» 가 아니라 «펌웨어가 boot_id 를
    새로 만들어서» 임을 못 박는다. 펌웨어가 `boot_id` 를 고정값으로 바꾸는 순간
    재부팅 후 링크가 조용히 죽는다.
    """
    decoder = ScanDecoder()
    boot = "3" * 16
    wire = [[0.0, 1000]]
    assert decoder.decode(
        encode_scan(seq=900, ts_ms=1, device_id="lidar-01", boot_id=boot, points_wire=wire)
    ).accepted
    result = decoder.decode(
        encode_scan(seq=1, ts_ms=2, device_id="lidar-01", boot_id=boot, points_wire=wire)
    )
    assert not result.accepted
    assert "seq" in result.reason


def test_mount_missing_warns_and_keeps_mock_defaults() -> None:
    """장착 보정 없이 뜨면 기본(0, +1)으로 돌되 경고한다 — 조용히 돌아간 지도 방지."""
    yaw, direction, warning = mount_from_env({})
    assert (yaw, direction) == (0.0, 1)
    assert "LIDAR_MOUNT_YAW_DEG" in warning and "LIDAR_ANGLE_DIRECTION" in warning

    _, _, partial = mount_from_env({"LIDAR_MOUNT_YAW_DEG": "270", "LIDAR_ANGLE_DIRECTION": " "})
    assert "LIDAR_ANGLE_DIRECTION" in partial and "LIDAR_MOUNT_YAW_DEG" not in partial


def test_mount_from_config_values_is_silent() -> None:
    assert mount_from_env({"LIDAR_MOUNT_YAW_DEG": "270", "LIDAR_ANGLE_DIRECTION": "-1"}) == (
        270.0,
        -1,
        "",
    )
