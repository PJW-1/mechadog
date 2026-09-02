"""메시지 스키마 정본 픽스처 검증.

보내는 쪽(Python)과 받는 쪽(C++ 펌웨어)을 서로 다른 사람이 작성하므로,
스키마 불일치는 "로봇이 조용히 반응하지 않는" 형태로 나타나고 서로
상대방 코드를 의심하게 된다.

그래서 프로세스 규칙 대신 **정본 픽스처**로 막는다.

  · tests/fixtures/protocol_samples.jsonl  — 유효 메시지 정본
  · tests/fixtures/protocol_invalid.jsonl  — 폐기·클램핑 대상

Python 직렬화(host/common/protocol.py)와 C++ 파서(command_parser)는
모두 이 픽스처를 기준으로 검증한다. 한쪽만 바꾸면 CI 가 실패한다.

WBS 3.1.1 구현 시 이 파일을 protocol.py 기반 테스트로 확장한다.
현재는 스키마 자체의 일관성만 검증한다 (구현 전 단계).
"""

import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLES = FIXTURES / "protocol_samples.jsonl"
INVALID = FIXTURES / "protocol_invalid.jsonl"
CONFIG = ROOT / "config" / "config.yaml"

# PRD FR-5.1 — 제어 명령 타입
KNOWN_TYPES = {"MOVE", "POSE", "GAIT", "STOP", "ACTION", "LED", "SOUND"}

# 타입별 필수 필드 (seq / ts / type 은 공통 필수)
REQUIRED_FIELDS = {
    "MOVE": {"step", "angle"},
    "POSE": {"pitch", "roll", "height", "dur"},
    "GAIT": {"lift_time", "ground_time", "height"},
    "STOP": set(),
    "ACTION": {"id"},
    "LED": {"color", "blink_hz"},
    "SOUND": {"phrase_id"},
}

# HW_MechDog API 허용 범위 (DR-1)
RANGES = {
    "step": (-100, 100),
    "angle": (-30, 30),
    "id": (0, 15),
}


def _load(path: Path) -> list[dict]:
    assert path.exists(), f"픽스처 없음: {path}"
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


@pytest.fixture(scope="module")
def samples() -> list[dict]:
    return _load(SAMPLES)


@pytest.fixture(scope="module")
def invalid() -> list[dict]:
    return _load(INVALID)


def test_samples_cover_every_known_type(samples: list[dict]) -> None:
    """정본 픽스처는 모든 명령 타입을 최소 1건씩 포함해야 한다.

    새 타입을 추가하면서 픽스처를 빠뜨리면 여기서 잡힌다.
    """
    covered = {m["type"] for m in samples}
    missing = KNOWN_TYPES - covered
    assert not missing, f"픽스처에 없는 타입: {sorted(missing)}"


def test_samples_have_common_required_fields(samples: list[dict]) -> None:
    """seq·ts·type 은 모든 명령의 공통 필수 필드다."""
    for m in samples:
        for key in ("seq", "ts", "type"):
            assert key in m, f"{m.get('_case')}: '{key}' 누락"


def test_samples_have_type_specific_fields(samples: list[dict]) -> None:
    for m in samples:
        required = REQUIRED_FIELDS[m["type"]]
        missing = required - set(m)
        assert not missing, f"{m['_case']}: {m['type']} 필수 필드 누락 {sorted(missing)}"


def test_samples_are_within_api_range(samples: list[dict]) -> None:
    """정본 픽스처는 HW_MechDog API 허용 범위 안에 있어야 한다 (DR-1)."""
    for m in samples:
        for field, (lo, hi) in RANGES.items():
            if field in m:
                assert lo <= m[field] <= hi, f"{m['_case']}: {field}={m[field]} 범위 이탈"


def test_samples_include_boundary_cases(samples: list[dict]) -> None:
    """경계값이 포함되어 있어야 클램핑 구현을 검증할 수 있다."""
    steps = [m["step"] for m in samples if "step" in m]
    angles = [m["angle"] for m in samples if "angle" in m]
    assert max(steps) == 100 and min(steps) == -100, "step 경계값 케이스 누락"
    assert max(angles) == 30 and min(angles) == -30, "angle 경계값 케이스 누락"


def test_seq_is_monotonic(samples: list[dict]) -> None:
    """seq 는 단조 증가해야 한다. 역전 폐기 로직(FR-1.6)의 기준이다."""
    seqs = [m["seq"] for m in samples]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs)), "seq 중복"


def test_invalid_fixture_declares_expected_handling(invalid: list[dict]) -> None:
    """폐기·클램핑 픽스처는 기대 동작을 명시해야 한다."""
    allowed = {"discard", "discard_warn", "clamp"}
    for m in invalid:
        assert m.get("_expect") in allowed, f"{m.get('_case')}: _expect 값이 잘못됨"


def test_unknown_type_case_exists(invalid: list[dict]) -> None:
    """미지 타입 폐기 케이스가 있어야 한다 (FR-5.1 규칙 ④).

    이 규칙이 새 타입 추가를 하위 호환으로 만들어 준다. 케이스가 사라지면
    확장 안전성의 근거가 검증되지 않는다.
    """
    unknown = [m for m in invalid if m.get("type") and m["type"] not in KNOWN_TYPES]
    assert unknown, "미지 타입 케이스 누락"
    for m in unknown:
        assert m["_expect"] == "discard_warn"


def test_led_colors_exist_in_config(samples: list[dict]) -> None:
    """LED 명령의 색상은 config 의 에스컬레이션 색상 목록에 있어야 한다.

    픽스처와 config 가 어긋나면 런타임에 알 수 없는 색이 전달된다.
    """
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    palette = {v for k, v in cfg["escalation"]["led"].items() if isinstance(v, str)}
    for m in samples:
        if m["type"] == "LED":
            assert m["color"] in palette, f"{m['_case']}: 알 수 없는 색상 '{m['color']}'"
