"""Observer delivery must preserve the exact primary MAP_POSE and its failure semantics."""

import runpy
from pathlib import Path

import pytest

from host.common.map_pose_link import MapPoseEncoder

send_pose_packet = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "docker/ros2/pose_bridge.py")
)["send_pose_packet"]


class SocketSpy:
    def __init__(self, failing_port=None):
        self.sent = []
        self.failing_port = failing_port

    def sendto(self, data, peer):
        self.sent.append((data, peer))
        if peer[1] == self.failing_port:
            raise OSError("injected destination failure")


def packet():
    return (
        MapPoseEncoder("mechdog-02", "a" * 16)
        .encode(ts_ms=100, x_m=1.2, y_m=-0.3, yaw_rad=0.4, valid=True)
        .encode("utf8")
    )


def test_observer_receives_identical_pose_after_primary():
    sock = SocketSpy()
    data = packet()
    assert send_pose_packet(sock, data, ("127.0.0.1", 5205), ("127.0.0.1", 5206)) is None
    assert sock.sent == [(data, ("127.0.0.1", 5205)), (data, ("127.0.0.1", 5206))]


def test_observer_failure_does_not_turn_primary_success_into_failure():
    sock = SocketSpy(5206)
    error = send_pose_packet(sock, packet(), ("127.0.0.1", 5205), ("127.0.0.1", 5206))
    assert error == "injected destination failure"
    assert len(sock.sent) == 2


def test_primary_failure_still_propagates_and_does_not_copy():
    sock = SocketSpy(5205)
    with pytest.raises(OSError):
        send_pose_packet(sock, packet(), ("127.0.0.1", 5205), ("127.0.0.1", 5206))
    assert len(sock.sent) == 1


@pytest.mark.parametrize("observer", [None, ("127.0.0.1", 5205)])
def test_disabled_or_same_destination_does_not_duplicate(observer):
    sock = SocketSpy()
    assert send_pose_packet(sock, packet(), ("127.0.0.1", 5205), observer) is None
    assert len(sock.sent) == 1
