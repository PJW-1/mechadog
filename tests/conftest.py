"""공통 테스트 픽스처 (ENGINEERING_GUIDE 2.3).

핵심 부품은 **가짜 시계**다. 시각을 주입 가능하게 만들었기 때문에 300ms
타임아웃을 실제로 기다리지 않고 0초에 검증할 수 있다. 이 원칙 하나가
pytest 로 닫을 수 있는 범위를 결정한다 (ENGINEERING_GUIDE 2.1).
"""

import json
import logging
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONFIG = ROOT / "config" / "config.yaml"


class FakeClock:
    """주입 가능한 가짜 시계. 단위는 epoch 밀리초다."""

    def __init__(self, start_ms: int = 1_756_800_000_000) -> None:
        self.ms = start_ms

    def __call__(self) -> int:
        """`system_clock_ms` 와 같은 모양이라 그대로 주입된다."""
        return self.ms

    def advance(self, ms: int) -> int:
        self.ms += ms
        return self.ms


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(autouse=True)
def _isolate_logging():
    """`mechadog` 로거 상태를 시험마다 되돌린다.

    ⚠️ **`setup_logging` 은 `propagate = False` 를 건다.** 실제 운용에서는 옳다 —
    루트로 새 나가면 누가 `basicConfig` 를 부른 순간 줄이 두 번 찍힌다. 그런데
    `caplog` 는 루트에 핸들러를 달아 잡으므로, 한 시험이 전파를 끊어 두면 **이후
    시험들이 아무 로그도 못 본다.**

    실제로 `test_logging.py` 를 넣은 뒤 `test_runtime.py` 의 로깅 시험이 단독으로는
    통과하고 전체 실행에서는 실패했다. 시험 간 누수는 이렇게 나타난다.
    """
    logger = logging.getLogger("mechadog")
    saved = (logger.handlers[:], logger.filters[:], logger.level, logger.propagate)
    yield
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.handlers, logger.filters, logger.level, logger.propagate = saved


@pytest.fixture(scope="session")
def cfg() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict]:
    """골든 픽스처 로더. 빈 줄은 건너뛴다."""
    assert path.exists(), f"픽스처 없음: {path}"
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
