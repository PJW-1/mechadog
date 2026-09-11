"""펌웨어 구동 빌드 환경 점검 도구 (`tools/firmware_env.py`).

벤더 파일은 저장소에도 시험에도 넣을 수 없다(ADR-20). 그래서 **가짜 파일과 그 해시로
만든 표**를 넣어 판정만 본다 — 실제 표가 맞는지는 개발 PC 의 점검이 본다.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tools import firmware_env
from tools.firmware_env import (
    SKETCH,
    check_core,
    check_ignored,
    check_libraries,
    check_vendor_files,
)


def _table(sketch: Path, files: dict[str, bytes]) -> dict[str, tuple[int, str]]:
    sketch.mkdir(parents=True, exist_ok=True)
    table = {}
    for name, body in files.items():
        (sketch / name).write_bytes(body)
        table[name] = (len(body), hashlib.sha256(body).hexdigest())
    return table


def test_vendor_files_pass_when_size_and_hash_match(tmp_path: Path) -> None:
    table = _table(tmp_path, {"HW_MechDog.h": b"a", "action.h": b"bb"})
    assert all(check.ok for check in check_vendor_files(tmp_path, table))


def test_missing_and_changed_vendor_files_are_named(tmp_path: Path) -> None:
    """⚠️ **다른 배포본은 틀린 것이 아니라 검증하지 않은 조합이다** — 그렇게 알린다."""
    table = _table(tmp_path, {"HW_MechDog.h": b"a", "action.h": b"bb"})
    (tmp_path / "action.h").write_bytes(b"cc")  # 크기는 같고 내용이 다르다
    table["Servo.h"] = (1, "0" * 64)
    problems = {check.item: check.problem for check in check_vendor_files(tmp_path, table)}
    assert problems["벤더 HW_MechDog.h"] == ""
    assert "해시 다름" in problems["벤더 action.h"]
    assert "없음" in problems["벤더 Servo.h"]


def test_vendor_names_are_ignored_by_git() -> None:
    """저장소의 `.gitignore` 가 실제로 벤더 파일을 막는다 — 복사 전에 확인할 수 있다."""
    names = list(firmware_env.VENDOR_FILES)
    assert check_ignored(SKETCH, names)[0].ok


def test_an_exposed_file_is_reported() -> None:
    check = check_ignored(SKETCH, ["firmware_mechdog_motion.ino"])[0]
    assert not check.ok and "firmware_mechdog_motion.ino" in check.problem


def _library(root: Path, folder: str, name: str, version: str) -> None:
    (root / folder).mkdir(parents=True)
    (root / folder / "library.properties").write_text(
        f"name={name}\nversion={version}\n", encoding="utf-8"
    )


def test_libraries_need_the_exact_version(tmp_path: Path) -> None:
    """SensorLib 은 이 버전 API 에 맞춰져 있다 — 새 버전이면 컴파일이 깨진다."""
    expected = {"SensorLib": ("SensorLib", "0.2.1"), "Madgwick": ("Madgwick", "1.2.0")}
    _library(tmp_path, "SensorLib", "SensorLib", "0.4.1")
    problems = {c.item: c.problem for c in check_libraries(tmp_path, expected)}
    assert "0.2.1 이 필요하다" in problems["라이브러리 SensorLib"]
    assert "없음" in problems["라이브러리 Madgwick"]

    (tmp_path / "SensorLib" / "library.properties").write_text(
        "name=SensorLib\nversion=0.2.1\n", encoding="utf-8"
    )
    assert check_libraries(tmp_path, {"SensorLib": ("SensorLib", "0.2.1")})[0].ok


def test_core_version_is_checked_by_folder(tmp_path: Path) -> None:
    assert not check_core(tmp_path)[0].ok
    (tmp_path / "packages" / "esp32" / "hardware" / "esp32" / firmware_env.ESP32_CORE).mkdir(
        parents=True
    )
    assert check_core(tmp_path)[0].ok


def test_cli_fails_on_an_empty_environment(tmp_path: Path, capsys) -> None:
    assert firmware_env.main(["--arduino-dir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "라이브러리 SensorLib" in out and "문제" in out
