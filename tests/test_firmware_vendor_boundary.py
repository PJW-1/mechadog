"""펌웨어와 벤더 라이브러리의 경계 (WBS 4.1.4 · ADR-20).

센서 HAL 과 벤더 구동 코드를 **한 빌드에서 함께 켠다.** 전제는 둘이 I2C 0번 포트
(SDA22/SCL23)를 나눠 쓰지 않는 것이다 — 벤더는 같은 포트를 `IIC1` 로 감싸지만
`homeostasis()`·`UltrasoundSonar`·`MP3Sensor` 에서만 연다. 우리 코드가 그 기능을
부르는 순간 두 드라이버가 한 버스를 붙잡는다 (`src/sensor_hal.h` 머리말).

벤더 소스는 저장소에 없으므로 CI 는 그 충돌을 컴파일로 알 수 없다. 그래서 **우리
쪽 소스를 읽어서 막는다.**
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKETCH = ROOT / "firmware_mechdog_motion"

#: `.gitignore` 의 벤더 파일 목록과 같다 — 로컬에 복사해 둔 벤더 파일은 검사 대상이 아니다.
VENDOR_STEMS = frozenset({"HW_MechDog", "Hiwonder", "Servo", "WMMatrixLed", "pwm_servo", "action"})

#: I2C 0번 포트를 여는 벤더 기능.
FORBIDDEN = ("homeostasis", "UltrasoundSonar", "MP3Sensor", "IIC1")


def _our_sources() -> list[Path]:
    sources = [SKETCH / "firmware_mechdog_motion.ino"]
    sources += sorted(
        path
        for path in (SKETCH / "src").rglob("*")
        if path.suffix in {".cpp", ".h"} and path.stem not in VENDOR_STEMS
    )
    return sources


def _code(path: Path) -> str:
    """주석을 걷어낸 본문. 경계를 **설명하는** 주석이 걸리면 안 된다."""
    text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def test_firmware_never_opens_the_vendor_i2c_features() -> None:
    offenders = [
        f"{path.relative_to(ROOT).as_posix()}: {name}"
        for path in _our_sources()
        for name in FORBIDDEN
        if re.search(rf"\b{name}\b", _code(path))
    ]
    assert offenders == [], "벤더 I2C 기능은 센서 HAL 의 버스와 충돌한다"


def test_motion_hal_is_the_only_door_to_the_vendor_library() -> None:
    """벤더 헤더를 여는 곳이 하나여야 위의 검사가 의미를 가진다."""
    including = [
        path.name
        for path in _our_sources()
        if re.search(r'#\s*include\s*[<"](?:[^<">]*/)?HW_MechDog\.h[>"]', _code(path))
    ]
    assert including == ["motion_hal.cpp"]
