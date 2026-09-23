#!/usr/bin/env python3
"""펌웨어 범위 가드 — 카메라 펌웨어와 로봇 펌웨어를 한 PR에서 같이 바꾸지 못하게 한다.

배경: firmware_xiao_vision 은 "촬영과 MJPEG 송출"만 담당하는 부품(DR-3)이다.
로봇 기능 PR에 카메라 코드가 끼어 들어가면(음성 마이크·LED 진단 등)
플래시할 때 로봇 기능까지 함께 올라가 실기가 죽는 사고가 났다 (2026-09-22~23).

규칙: 하나의 변경이 코드 파일(*.ino/*.cpp/*.h/*.c)을
  카메라(firmware_xiao_vision/) 와 로봇 펌웨어(firmware_mechdog_motion/,
  firmware_lidar_relay/) 양쪽에서 바꾸면 실패한다.
문서(.md)·설정 동반 변경은 허용 — 위험한 것은 코드 결합이다.

사용:
  python tools/check_firmware_scope.py --base origin/dev        # PR diff 검사
  python tools/check_firmware_scope.py --commit 03e2df7         # 특정 커밋 검사
  git diff --name-only A B | python tools/check_firmware_scope.py --stdin
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import PurePosixPath

CAMERA_DIR = "firmware_xiao_vision/"
ROBOT_DIRS = ("firmware_mechdog_motion/", "firmware_lidar_relay/")
CODE_SUFFIXES = {".ino", ".cpp", ".c", ".h", ".hpp", ".cc"}


def classify(path: str) -> str | None:
    """코드 파일이면 'camera'/'robot', 문서 등 비코드·기타 경로는 None."""
    if PurePosixPath(path).suffix.lower() not in CODE_SUFFIXES:
        return None
    if path.startswith(CAMERA_DIR):
        return "camera"
    if any(path.startswith(d) for d in ROBOT_DIRS):
        return "robot"
    return None


def evaluate(files: list[str]) -> tuple[list[str], list[str]]:
    """변경 파일 목록을 (카메라 코드, 로봇 코드)로 나눈다."""
    camera = [f for f in files if classify(f) == "camera"]
    robot = [f for f in files if classify(f) == "robot"]
    return camera, robot


def changed_files(base: str | None, commit: str | None, use_stdin: bool) -> list[str]:
    if use_stdin:
        return [ln.strip() for ln in sys.stdin if ln.strip()]
    rng = f"{commit}^!" if commit else f"{base}...HEAD"
    out = subprocess.run(
        ["git", "diff", "--name-only", rng],
        capture_output=True,
        text=True,
        check=True,
    )
    return [ln for ln in out.stdout.splitlines() if ln]


def main() -> int:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--base", help="머지 대상 브랜치 (예: origin/dev)")
    src.add_argument("--commit", help="단일 커밋 검사")
    src.add_argument("--stdin", action="store_true", help="파일 목록을 stdin으로")
    args = ap.parse_args()

    camera, robot = evaluate(changed_files(args.base, args.commit, args.stdin))
    if camera and robot:
        print("FAIL — 카메라와 로봇 펌웨어 코드가 한 변경에 섞여 있습니다.", file=sys.stderr)
        print("  camera:", *("    " + f for f in camera), sep="\n", file=sys.stderr)
        print("  robot:", *("    " + f for f in robot), sep="\n", file=sys.stderr)
        print(
            "  firmware_xiao_vision 은 DR-3 상 촬영·송출 전용이다. "
            "로봇 기능과 같은 PR로 넣지 말고 단독 PR로 분리한다.",
            file=sys.stderr,
        )
        return 1

    print(f"ok - camera {len(camera)}건, robot {len(robot)}건 (동시 변경 없음)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
