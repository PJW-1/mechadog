"""의존성 선언 검증.

CI 러너(ubuntu)와 개발 PC(Windows)의 플랫폼이 다르므로, 플랫폼 전용
패키지는 반드시 PEP 508 환경 마커로 분기해야 한다. 마커가 없으면
리눅스 러너에서 설치가 실패하여 CI 전체가 멈춘다.

실제로 `onnxruntime-directml` 을 마커 없이 선언해 CI가 실패한 적이 있다.
같은 실수를 반복하지 않도록 여기서 막는다.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQ = ROOT / "requirements.txt"
REQ_DEV = ROOT / "requirements-dev.txt"

# 특정 플랫폼에서만 휠이 제공되는 패키지 → 마커 필수
PLATFORM_SPECIFIC = (
    "onnxruntime-directml",  # Windows only (DirectML)
    "onnxruntime-gpu",  # Linux/Windows only (CUDA)
    "onnxruntime-openvino",
    "pywin32",
    "tensorflow-metal",  # macOS only
)


def _lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_requirements_files_exist() -> None:
    assert REQ.exists(), f"없음: {REQ}"
    assert REQ_DEV.exists(), f"없음: {REQ_DEV}"


def test_dev_requirements_includes_runtime() -> None:
    """개발 환경은 런타임 의존성을 포함해야 한다."""
    assert any(line.startswith("-r requirements.txt") for line in _lines(REQ_DEV))


@pytest.mark.parametrize("pkg", PLATFORM_SPECIFIC)
def test_platform_specific_packages_have_marker(pkg: str) -> None:
    """플랫폼 전용 패키지에는 환경 마커(;)가 붙어 있어야 한다."""
    for line in _lines(REQ):
        if line.lower().startswith(pkg):
            assert ";" in line, (
                f"'{pkg}' 는 플랫폼 전용이므로 환경 마커가 필요하다. "
                f'예: {pkg}>=1.19 ; sys_platform == "win32"'
            )


def test_onnxruntime_has_both_platforms() -> None:
    """Windows(DirectML)와 그 외(CPU) 양쪽 경로가 선언되어 있어야 한다.

    한쪽만 있으면 반대 플랫폼에서 추론 런타임이 아예 설치되지 않는다.
    """
    body = REQ.read_text(encoding="utf-8")
    assert 'sys_platform == "win32"' in body
    assert 'sys_platform != "win32"' in body
