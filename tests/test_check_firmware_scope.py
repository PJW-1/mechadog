"""firmware_xiao_vision(카메라)와 로봇 펌웨어 코드가 한 변경에 섞이면 실패한다.

사고 배경: 로봇 기능 PR에 카메라 코드가 끼어 플래시 시 로봇 기능까지 함께
올라가는 사고를 막는다 (DR-3 — 카메라는 촬영·송출 전용).
"""

from __future__ import annotations

from tools.check_firmware_scope import evaluate


def test_camera_and_robot_code_together_fails():
    camera, robot = evaluate(
        [
            "firmware_xiao_vision/firmware_xiao_vision.ino",
            "firmware_mechdog_motion/firmware_mechdog_motion.ino",
        ]
    )
    assert camera and robot


def test_camera_docs_and_robot_code_passes():
    # 문서 동반 변경은 허용 — 위험한 것은 코드 결합이다.
    camera, robot = evaluate(
        [
            "firmware_xiao_vision/README.md",
            "firmware_mechdog_motion/src/sensor_hal.cpp",
        ]
    )
    assert not camera and robot


def test_camera_code_alone_passes():
    camera, robot = evaluate(["firmware_xiao_vision/firmware_xiao_vision.ino"])
    assert camera and not robot


def test_lidar_relay_counts_as_robot():
    camera, robot = evaluate(
        [
            "firmware_xiao_vision/firmware_xiao_vision.ino",
            "firmware_lidar_relay/firmware_lidar_relay.ino",
        ]
    )
    assert camera and robot


def test_headers_and_diagnostic_sketches_count_as_code():
    camera, robot = evaluate(
        [
            "firmware_xiao_vision/diagnostics/mic_probe/mic_probe.ino",
            "firmware_mechdog_motion/src/sensor_hal.h",
        ]
    )
    assert camera and robot
