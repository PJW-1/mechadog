"""firmware/xiao_vision(카메라)와 로봇 펌웨어 코드가 한 변경에 섞이면 실패한다.

사고 배경: 로봇 기능 PR에 카메라 코드가 끼어 플래시 시 로봇 기능까지 함께
올라가는 사고를 막는다 (DR-3 — 카메라는 촬영·송출 전용).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.dev.check_firmware_scope import changed_files, evaluate


def test_camera_and_robot_code_together_fails():
    camera, robot = evaluate(
        [
            "firmware/xiao_vision/xiao_vision.ino",
            "firmware/mechdog_motion/mechdog_motion.ino",
        ]
    )
    assert camera and robot


def test_camera_docs_and_robot_code_passes():
    # 문서 동반 변경은 허용 — 위험한 것은 코드 결합이다.
    camera, robot = evaluate(
        [
            "firmware/xiao_vision/README.md",
            "firmware/mechdog_motion/src/sensor_hal.cpp",
        ]
    )
    assert not camera and robot


def test_camera_code_alone_passes():
    camera, robot = evaluate(["firmware/xiao_vision/xiao_vision.ino"])
    assert camera and not robot


def test_lidar_relay_counts_as_robot():
    camera, robot = evaluate(
        [
            "firmware/xiao_vision/xiao_vision.ino",
            "firmware/lidar_relay/lidar_relay.ino",
        ]
    )
    assert camera and robot


def test_headers_and_diagnostic_sketches_count_as_code():
    camera, robot = evaluate(
        [
            "firmware/xiao_vision/diagnostics/mic_probe/mic_probe.ino",
            "firmware/mechdog_motion/src/sensor_hal.h",
        ]
    )
    assert camera and robot


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_pure_move_is_not_a_code_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # 폴더를 옮기기만 한 카메라 코드는 플래시되는 것이 같다 — 고친 로봇 코드만 남아야 한다.
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    for rel, text in (("cam/cam.ino", "void setup() {}\n"), ("dog/dog.ino", "int a = 1;\n")):
        (tmp_path / rel).parent.mkdir(parents=True)
        (tmp_path / rel).write_text(text)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "branch", "base")
    (tmp_path / "firmware").mkdir()
    _git(tmp_path, "mv", "cam", "firmware/xiao_vision")
    _git(tmp_path, "mv", "dog", "firmware/mechdog_motion")
    (tmp_path / "firmware/mechdog_motion/dog.ino").write_text("int a = 2;\n")
    _git(tmp_path, "commit", "-qam", "move")
    monkeypatch.chdir(tmp_path)

    camera, robot = evaluate(changed_files("base", None, False))
    assert not camera and robot == ["firmware/mechdog_motion/dog.ino"]
