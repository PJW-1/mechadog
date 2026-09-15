"""펌웨어 구동 빌드 환경 점검 (WBS 4.1.4 · 5.1.2 · ADR-20).

    python tools/firmware_env.py
    python tools/firmware_env.py --arduino-dir C:/Users/<나>/.arduino-mechdog

**벤더 파일은 저장소에 없다** — 라이선스 표기가 없어 재배포할 수 없다(ADR-20). 각자 받아
스케치 폴더에 두는데, 손으로 하면 세 가지가 빠진다: **같은 버전인가 · 빠진 파일이 없나 ·
git 이 무시하고 있나.** 마지막이 빠지면 `git add .` 한 번에 공개 저장소로 올라간다.

`fetch_models.py` 가 가중치에 하는 일을 벤더 파일에 한다 — 파일은 싣지 않고 **크기와
SHA-256 만** 기록해 대조한다. 준비 절차는 `firmware_mechdog_motion/README.md` 의
*"구동·센서 통합 빌드 준비"* 절이다.

⚠️ **점검만 한다.** 받지도, 복사하지도, 설치하지도 않는다.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKETCH = ROOT / "firmware_mechdog_motion"

sys.path.insert(0, str(ROOT))

from host.common.console import survive_encoding_errors  # noqa: E402

#: 스케치 폴더에 둘 벤더 파일 → (크기, SHA-256). **2026-09-12 에 센서+구동 빌드를 통과한 조합이다.**
#:
#: ⚠️ 다른 배포본을 받으면 해시가 달라진다. 틀렸다는 뜻이 아니라 **검증하지 않은 조합**이라는
#: 뜻이므로, 통합 빌드와 실기 확인을 다시 거친 뒤 이 표를 갱신한다.
VENDOR_FILES: dict[str, tuple[int, str]] = {
    "HW_MechDog.cpp": (9975, "896b373cb69c192cc70cb81f60cc8e71bacd2c978a6e88cada3b6b06924c1719"),
    "HW_MechDog.h": (2264, "c6aecc088979eae41b97e2d0a68bb254a3d8591556d5017a1f84abeba3d63036"),
    "Hiwonder.cpp": (11058, "62a9935a6edc21a83688626c8e0ee01f6f593eb9bdbb9e6cde538912b88a5a72"),
    "Hiwonder.h": (6322, "3b5860f24d237d74730d5017b72ab210406e656903dd690f8e2a75df617a0e02"),
    "Servo.cpp": (2057, "84d404f8d56c5dd147d452dcc3bb3371778e6d00c3866993ba6a4de34e905ff7"),
    "Servo.h": (1676, "fa5c777bb0d7d2c1bd9ad529d75e4f5dc5b29b6f7f66db4b16f5fa4ec0c068ac"),
    "WMMatrixLed.cpp": (20135, "666e397e6d4da35f957c27d17a3d5be93bbd8654e1996fc2d7a03942b77ec4cc"),
    "WMMatrixLed.h": (6576, "e70276127fb5757b6058afb4552a89e1e889bc134756b98a960e266e2d962d1a"),
    "pwm_servo.cpp": (12076, "5ff038eb597cce36009f772f5417c1ddcaa2960223f2fa403a6bc84371da259d"),
    "pwm_servo.h": (844, "d726a9baddbcc0067baffdef5235f0c112ba2dc81893cb87e2abd46d8322161b"),
    "action.h": (7903, "caba024d6034a1868f7e360d3d3259077aabbf3c4cb598419a54e05d13a7dfc1"),
}

#: Arduino 라이브러리 폴더 이름 → `library.properties` 의 (name, version).
LIBRARIES: dict[str, tuple[str, str]] = {
    # 벤더 — 미리 컴파일된 보행 계산. 벤더 헤더가 MPU6050 도 부른다(MIT).
    "MechDog_Arduino": ("mechdog", "1.2.5"),
    "MPU6050": ("MPU6050", "1.3.1"),
    # 센서 HAL — QMI8658 드라이버는 **이 버전 API** 에 맞춰져 있다. 자세 필터는 Madgwick.
    "SensorLib": ("SensorLib", "0.2.1"),
    "Madgwick": ("Madgwick", "1.2.0"),
}

#: 구동 빌드가 고정한 보드 패키지 버전 (펌웨어 README "기본 빌드").
ESP32_CORE = "2.0.12"

#: 격리 설치 위치의 기본값 — 쓰던 Arduino IDE 설정을 건드리지 않는다.
DEFAULT_ARDUINO_DIR = Path.home() / ".arduino-mechdog"


@dataclass(frozen=True, slots=True)
class Check:
    """점검 한 줄. `problem` 이 비어 있으면 통과다."""

    item: str
    problem: str = ""

    @property
    def ok(self) -> bool:
        return not self.problem


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_vendor_files(
    sketch: Path, expected: Mapping[str, tuple[int, str]] = VENDOR_FILES
) -> list[Check]:
    checks = []
    for name, (size, digest) in expected.items():
        path = sketch / name
        if not path.is_file():
            checks.append(Check(f"벤더 {name}", "없음 — 스케치 폴더에 복사한다"))
        elif path.stat().st_size != size or _sha256(path) != digest:
            checks.append(Check(f"벤더 {name}", "해시 다름 — 검증하지 않은 배포본이다"))
        else:
            checks.append(Check(f"벤더 {name}"))
    return checks


def check_ignored(sketch: Path, names: list[str], root: Path = ROOT) -> list[Check]:
    """⚠️ **벤더 파일이 git 에서 무시되는가.** 아니면 공개 저장소로 올라간다."""
    exposed = []
    for name in names:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str((sketch / name).relative_to(root))],
            cwd=root,
            check=False,
        )
        if result.returncode != 0:
            exposed.append(name)
    if exposed:
        return [Check("git 무시", f"무시되지 않음: {', '.join(exposed)} — .gitignore 를 확인한다")]
    return [Check("git 무시")]


def _properties(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def check_libraries(
    libraries_dir: Path, expected: Mapping[str, tuple[str, str]] = LIBRARIES
) -> list[Check]:
    checks = []
    for folder, (name, version) in expected.items():
        props = libraries_dir / folder / "library.properties"
        if not props.is_file():
            checks.append(Check(f"라이브러리 {folder}", f"없음 — {libraries_dir}"))
            continue
        found = _properties(props)
        if (found.get("name"), found.get("version")) != (name, version):
            got = f"{found.get('name')} {found.get('version')}"
            checks.append(Check(f"라이브러리 {folder}", f"{got} — {name} {version} 이 필요하다"))
        else:
            checks.append(Check(f"라이브러리 {folder}"))
    return checks


def check_core(data_dir: Path, version: str = ESP32_CORE) -> list[Check]:
    if (data_dir / "packages" / "esp32" / "hardware" / "esp32" / version).is_dir():
        return [Check(f"보드 패키지 esp32 {version}")]
    return [Check(f"보드 패키지 esp32 {version}", f"없음 — {data_dir}")]


def run(arduino_dir: Path, sketch: Path = SKETCH) -> list[Check]:
    return [
        *check_vendor_files(sketch),
        *check_ignored(sketch, [name for name in VENDOR_FILES if (sketch / name).exists()]),
        *check_libraries(arduino_dir / "user" / "libraries"),
        *check_core(arduino_dir / "data"),
    ]


def main(argv: list[str] | None = None) -> int:
    survive_encoding_errors()
    parser = argparse.ArgumentParser(prog="firmware_env", description=__doc__)
    parser.add_argument(
        "--arduino-dir",
        type=Path,
        default=Path(os.environ.get("MECHDOG_ARDUINO_DIR", DEFAULT_ARDUINO_DIR)),
        help="격리 설치 폴더 (data/ · user/ 를 담는다). 기본 ~/.arduino-mechdog",
    )
    args = parser.parse_args(argv)
    checks = run(args.arduino_dir)
    for check in checks:
        print(
            f"  {'OK ' if check.ok else 'XX '} {check.item}"
            + (f"  {check.problem}" if check.problem else "")
        )
    failed = sum(not check.ok for check in checks)
    print(
        f"\n{'모두 준비됨' if not failed else f'{failed}개 문제'} — 절차: firmware_mechdog_motion/README.md"
    )
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
