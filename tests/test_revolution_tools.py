"""실기 도구의 조각 수신: 안전은 패킷마다, 지도 입력은 완성된 바퀴마다."""

from __future__ import annotations

import json
from collections import deque
from unittest.mock import Mock

import pytest

from host.behavior.commander import Commander
from host.behavior.patrol import controller_from_config
from host.behavior.zones import ZoneStore
from host.common.lidar_link import ScanDecoder, encode_scan, scan_of
from host.common.protocol import CommandEncoder
from host.slam.occupancy import OccupancyGrid
from tools.lidar import lidar_slam
from tools.ops import patrol_run


def packets(revolutions=1, *, device="lidar-test", boot="boot", first_seq=1, danger=False):
    """실측과 같이 한 바퀴를 71점씩 7패킷으로 나눈다."""
    result = []
    for index in range(7 * revolutions):
        points = [[(index % 7 * 71 + point) * 360 / 497, 2000] for point in range(71)]
        if danger and index == 0:
            points[0][1] = 50
        result.append(
            encode_scan(
                seq=first_seq + index,
                ts_ms=1000 + index * 14,
                device_id=device,
                boot_id=boot,
                points_wire=points,
            ).encode()
        )
    return result


class PacketSocket:
    def __init__(self, raw=(), *, empty_error=TimeoutError, on_receive=None):
        self.queue = deque(raw)
        self.empty_error = empty_error
        self.on_receive = on_receive
        self.received = 0
        self.sent = []

    def recvfrom(self, _size):
        if not self.queue:
            raise self.empty_error
        raw = self.queue.popleft()
        self.received += 1
        if self.on_receive is not None:
            self.on_receive(self.received)
        return raw, ("127.0.0.1", 1)

    def sendto(self, raw, _peer):
        self.sent.append(raw)
        return len(raw)

    def close(self):
        pass


def decoded_points(raw):
    decoder = ScanDecoder()
    return tuple(point for packet in raw for point in scan_of(decoder.decode(packet)).points)


@pytest.mark.parametrize("part_count", [6, 7])
def test_patrol_observes_only_complete_revolutions(monkeypatch, cfg, part_count):
    controller, scan_sock, _ = run_patrol(monkeypatch, cfg, packets()[:part_count])
    assert controller.guard_scan.call_count == part_count
    assert controller.observe_obstacle_scan.call_count == (part_count == 7)
    controller.observe_scan.assert_not_called()
    if part_count == 7:
        scan, received_ms = controller.observe_obstacle_scan.call_args.args
        assert scan.points == decoded_points(packets())
        assert scan.seq == 7
        assert received_ms == 1000 + scan_sock.received * 14


def run_patrol(monkeypatch, cfg, raw, *, on_receive=None):
    """serve_real 자체를 실행하되 모든 소켓·시간·ODOM은 격리한다."""
    # 합성 패킷은 로봇 기준 각도다. 세션 공용 cfg는 변경하지 않는다.
    config = {**cfg, "lidar": {**cfg["lidar"], "mount_yaw_deg": 0, "angle_direction": 1}}
    scan_sock = PacketSocket(raw, empty_error=BlockingIOError, on_receive=on_receive)
    empty_sock = PacketSocket(empty_error=BlockingIOError)
    cmd_sock = PacketSocket()
    forward_sock = PacketSocket()
    sockets = iter([scan_sock, empty_sock, empty_sock])
    monkeypatch.setattr(patrol_run, "open_socket", lambda _port: next(sockets))
    monkeypatch.setattr(patrol_run.socket, "socket", lambda *_args: cmd_sock)
    monkeypatch.setattr(patrol_run, "open_forward_socket", lambda: forward_sock)
    monkeypatch.setattr(patrol_run, "forward_peer_of", lambda _lidar: ("127.0.0.1", 2))
    monkeypatch.setattr(patrol_run, "open_odom_sender", lambda *_args: None)
    monkeypatch.setattr(patrol_run, "system_clock_ms", lambda: 1000 + scan_sock.received * 14)
    monkeypatch.setattr(patrol_run.time, "sleep", lambda _seconds: None)
    controller = controller_from_config(
        config,
        Commander(CommandEncoder()),
        OccupancyGrid.blank(resolution=0.05, span_cells=20),
        ZoneStore(("A",)),
        None,
    )
    controller.stats.cycles = 1
    controller.guard_scan = Mock(wraps=controller.guard_scan)
    controller.observe_obstacle_scan = Mock(wraps=controller.observe_obstacle_scan)
    controller.observe_scan = Mock()
    controller.step = Mock(return_value=())
    args = patrol_run.build_parser().parse_args(
        [
            "--device",
            "mechdog-02",
            "--lidar-device",
            "lidar-test",
            "--robot",
            "127.0.0.1",
            "--cycles",
            "1",
        ]
    )
    assert patrol_run.serve_real(args, config, controller, None) == 0
    assert forward_sock.sent == raw
    return controller, scan_sock, cmd_sock


def test_patrol_sends_estop_before_receiving_second_packet(monkeypatch, cfg):
    # send()의 실제 직렬화 결과와 recvfrom 순서를 함께 기록한다.
    events = []
    original_send = PacketSocket.sendto

    def record_send(self, raw, peer):
        events.append(("send", json.loads(raw)["type"]))
        return original_send(self, raw, peer)

    monkeypatch.setattr(PacketSocket, "sendto", record_send)
    controller, _, _ = run_patrol(
        monkeypatch,
        cfg,
        packets(danger=True),
        on_receive=lambda count: events.append(("receive", count)),
    )
    assert events.index(("receive", 1)) < events.index(("send", "ESTOP"))
    assert events.index(("send", "ESTOP")) < events.index(("receive", 2))
    assert controller.guard_scan.call_count == 7
    assert controller.observe_obstacle_scan.call_count == 1


@pytest.mark.parametrize("batch_size", [1, 3])
def test_slam_counts_revolutions_not_packets(monkeypatch, batch_size):
    raw = packets(batch_size + 1)
    sock = PacketSocket(raw)
    monkeypatch.setattr(lidar_slam, "system_clock_ms", lambda: sock.received * 14)
    batch = lidar_slam.collect_real(sock, ScanDecoder(), batch_size, "lidar-test")
    assert sock.received == batch_size * 7
    assert len(batch) == batch_size
    for index, scan in enumerate(batch):
        assert scan.seq == (index + 1) * 7
        assert scan.points == decoded_points(raw[index * 7 : (index + 1) * 7])


def test_slam_timeout_returns_only_completed_revolutions(monkeypatch):
    sock = PacketSocket(packets(2)[:10])
    monkeypatch.setattr(lidar_slam, "system_clock_ms", lambda: sock.received * 14)
    batch = lidar_slam.collect_real(sock, ScanDecoder(), 3, "lidar-test")
    assert [scan.seq for scan in batch] == [7]
    assert len(batch[0].points) == 497


def test_slam_does_not_carry_fragments_between_captures(monkeypatch):
    raw = packets()
    decoder = ScanDecoder()
    monkeypatch.setattr(lidar_slam, "system_clock_ms", lambda: 1000)
    assert lidar_slam.collect_real(PacketSocket(raw[:4]), decoder, 1) == []
    assert lidar_slam.collect_real(PacketSocket(raw[4:]), decoder, 1) == []


@pytest.mark.parametrize("change", ["boot", "device", "age"])
def test_slam_discards_fragments_across_restarts_or_expiry(monkeypatch, change):
    old = packets()[:4]
    new = packets(
        boot="new-boot" if change == "boot" else "boot",
        device="new-device" if change == "device" else "lidar-test",
        first_seq=5,
    )
    sock = PacketSocket(old + new)
    monkeypatch.setattr(
        lidar_slam,
        "system_clock_ms",
        lambda: sock.received * 14 + (301 if change == "age" and sock.received > 4 else 0),
    )
    batch = lidar_slam.collect_real(sock, ScanDecoder(), 1)
    assert sock.received == 11
    assert len(batch) == 1
    assert batch[0].points == decoded_points(new)
    assert batch[0].seq == 11


def test_slam_ignores_foreign_invalid_and_duplicate_packets(monkeypatch):
    raw = packets()
    sock = PacketSocket([raw[0], b"not json", raw[0], *packets(device="other"), *raw[1:]])
    monkeypatch.setattr(lidar_slam, "system_clock_ms", lambda: sock.received * 14)
    batch = lidar_slam.collect_real(sock, ScanDecoder(), 1, "lidar-test")
    assert sock.received == 16
    assert len(batch) == 1
    assert batch[0].points == decoded_points(raw)
