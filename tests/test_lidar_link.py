"""LiDAR 링크 규약 검증 — 골든 픽스처로 규칙 ①~⑥ 을 확인한다.

정본은 문서가 아니라 **픽스처**다 (PROTOCOL.md 6절과 같은 원칙).

  · tests/fixtures/lidar_samples.jsonl  — 유효 스캔 정본 (경계값 포함)
  · tests/fixtures/lidar_invalid.jsonl  — 폐기·부분수락 대상 + 기대 동작

기대값을 시험 코드에 적지 않고 픽스처의 `_expect` 에서 읽는 이유 —
**중계 노드(C++)가 같은 파일을 물고 검증해야 하기 때문이다.** 기대값이 파이썬
코드에만 있으면 펌웨어는 대조할 것이 없다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from host.common.lidar_link import (
    MAX_POINTS,
    SCAN_REQUIRED,
    Scan,
    ScanDecoder,
    encode_scan,
    points_from_wire,
    scan_of,
)
from host.common.protocol import Verdict
from host.slam.settings import read_lidar_section

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLES = FIXTURES / "lidar_samples.jsonl"
INVALID = FIXTURES / "lidar_invalid.jsonl"

META = ("_case", "_expect")


def load_jsonl(path: Path) -> list[dict]:
    assert path.exists(), f"픽스처 없음: {path}"
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def strip(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in META}


@pytest.fixture(scope="module")
def samples() -> list[dict]:
    return load_jsonl(SAMPLES)


@pytest.fixture(scope="module")
def invalid() -> list[dict]:
    return load_jsonl(INVALID)


@pytest.fixture(scope="module")
def lidar_cfg() -> dict:
    """`config.yaml` 의 `lidar` 절. **픽스처와 교차 검증하는 대상이다.**"""
    return read_lidar_section()


# ══════════════════════════════════════════════════════════════
#  픽스처 자체의 일관성
# ══════════════════════════════════════════════════════════════


def test_every_sample_has_a_case_note(samples: list[dict]) -> None:
    """설명 없는 픽스처는 왜 있는지 알 수 없어 아무도 못 고친다."""
    for row in samples:
        assert row.get("_case"), row


def test_every_sample_carries_required_fields(samples: list[dict]) -> None:
    for row in samples:
        missing = [name for name in SCAN_REQUIRED if name not in row]
        assert not missing, f"{row['_case']}: {missing}"


def test_invalid_rows_declare_expectations(invalid: list[dict]) -> None:
    allowed = {"discard", "discard_warn", "accept_dropped"}
    for row in invalid:
        assert row.get("_expect") in allowed, row


def test_range_boundaries_appear_in_fixtures(samples: list[dict], lidar_cfg: dict) -> None:
    """**교차 검증** — 설정의 유효 거리 경계가 픽스처에 정확히 등장해야 한다.

    팀장의 `test_telemetry_fixtures.py` 가 안전 임계에 대해 하는 것과 같은
    장치다. 이것이 없으면 `range_min_mm` 을 고쳐도 경계 동작이 한 번도
    검증되지 않는다.
    """
    distances = {point[1] for row in samples for point in row["points"]}
    assert lidar_cfg["range_min_mm"] in distances, "range_min_mm 경계 픽스처가 없다"
    assert lidar_cfg["range_max_mm"] in distances, "range_max_mm 경계 픽스처가 없다"


# ══════════════════════════════════════════════════════════════
#  규칙 ①~⑥
# ══════════════════════════════════════════════════════════════


def test_all_samples_are_accepted(samples: list[dict]) -> None:
    """유효 정본 전 라인이 파싱된다. 한 줄이라도 막히면 실기에서 막힌다."""
    decoder = ScanDecoder()
    for row in samples:
        result = decoder.validate(strip(row))
        assert result.accepted, f"{row['_case']}: {result.reason}"


def test_invalid_rows_behave_as_declared(invalid: list[dict]) -> None:
    """픽스처가 선언한 대로 동작한다. **순서대로 넣는다** — 규칙 ① 이 상태를 갖는다."""
    decoder = ScanDecoder()
    for row in invalid:
        expect = row["_expect"]
        result = decoder.validate(strip(row))
        if expect == "discard":
            assert result.verdict is Verdict.DISCARD, f"{row['_case']}: {result.verdict}"
        elif expect == "discard_warn":
            assert result.verdict is Verdict.DISCARD_WARN, f"{row['_case']}: {result.verdict}"
        else:
            scan = scan_of(result)
            assert scan is not None, f"{row['_case']}: 수락되어야 한다"
            assert scan.dropped >= 1, f"{row['_case']}: 버린 점이 세어져야 한다"
            assert scan.points, f"{row['_case']}: 나머지 점은 살아야 한다"


def test_parse_failure_does_not_refresh_the_link() -> None:
    """규칙 ③ — 깨진 패킷을 '살아 있음'으로 세면 페일세이프가 안 걸린다."""
    decoder = ScanDecoder()
    for raw in (b'{"seq": 1, "ts":', b"not json at all", b"[]", b'"string"'):
        result = decoder.decode(raw)
        assert not result.accepted
        assert not result.refreshes_link


def test_unknown_type_warns_but_malformed_type_does_not() -> None:
    """④ 와 ⑤ 를 구분한다 — WARN 은 '상대가 새 타입을 쓴다'는 신호 채널이다."""
    decoder = ScanDecoder()
    base = {
        "seq": 1,
        "ts": 1,
        "device_id": "lidar-a",
        "boot_id": "b",
        "points": [[0.0, 1000]],
    }
    assert decoder.validate({**base, "type": "SCAN_V9"}).verdict is Verdict.DISCARD_WARN
    for malformed in ([], 7, None, {}):
        result = decoder.validate({**base, "type": malformed})
        assert result.verdict is Verdict.DISCARD, malformed
        assert not result.warns


def test_seq_gate_is_per_device_and_boot() -> None:
    """규칙 ① — 개체·부팅별로 따로 센다. 한 카운터로 묶으면 서로를 폐기한다."""
    decoder = ScanDecoder()

    def feed(device: str, boot: str, seq: int) -> bool:
        return decoder.validate(
            {
                "seq": seq,
                "ts": seq,
                "type": "SCAN",
                "device_id": device,
                "boot_id": boot,
                "points": [[0.0, 1000]],
            }
        ).accepted

    assert feed("lidar-a", "boot-1", 5)
    assert not feed("lidar-a", "boot-1", 5), "중복은 폐기"
    assert not feed("lidar-a", "boot-1", 4), "역전은 폐기"
    assert feed("lidar-a", "boot-1", 6)
    # 재부팅 — 새 boot_id 의 seq=1 은 수락한다. 이것이 막히면 재부팅한 노드가
    # 이전 최대 seq 를 넘을 때까지 통째로 무시된다.
    assert feed("lidar-a", "boot-2", 1)
    # 다른 중계 노드는 완전히 독립이다 (LIDAR NODE 수량 2).
    assert feed("lidar-b", "boot-1", 1)


def test_points_over_the_cap_are_discarded() -> None:
    decoder = ScanDecoder()
    row = {
        "seq": 1,
        "ts": 1,
        "type": "SCAN",
        "device_id": "lidar-a",
        "boot_id": "b",
        "points": [[0.0, 1000]] * (MAX_POINTS + 1),
    }
    assert decoder.validate(row).verdict is Verdict.DISCARD


@pytest.mark.parametrize(
    "point",
    (
        [0.0, 1000.5],
        [0.0, 1000, -1],
        [0.0, 1000, 256],
        [0.0, 1000, 12.5],
    ),
)
def test_point_types_and_quality_range_follow_the_wire_schema(point: list[float]) -> None:
    decoder = ScanDecoder()
    result = decoder.validate(
        {
            "seq": 1,
            "ts": 1,
            "type": "SCAN",
            "device_id": "lidar-a",
            "boot_id": "b",
            "points": [point, [90.0, 1200, 100]],
        }
    )
    scan = scan_of(result)
    assert scan is not None
    assert scan.dropped == 1
    assert len(scan.points) == 1


def test_empty_scan_is_accepted() -> None:
    """점이 없는 것은 링크 문제가 아니라 센서가 아무것도 못 본 것이다."""
    decoder = ScanDecoder()
    result = decoder.validate(
        {
            "seq": 1,
            "ts": 1,
            "type": "SCAN",
            "device_id": "a",
            "boot_id": "b",
            "points": [],
        }
    )
    scan = scan_of(result)
    assert scan is not None
    assert scan.points == ()


# ══════════════════════════════════════════════════════════════
#  단위 경계
# ══════════════════════════════════════════════════════════════


def test_wire_units_are_converted_once() -> None:
    """전선은 deg·mm 이고 내부는 rad·m 다 (`units.py`)."""
    points = points_from_wire([[0.0, 1000], [90.0, 2500], [180.0, 500]])
    assert points[0] == pytest.approx((0.0, 1.0))
    assert points[1][0] == pytest.approx(1.5707963, abs=1e-6)
    assert points[1][1] == pytest.approx(2.5)
    assert points[2][1] == pytest.approx(0.5)


def test_angles_are_folded_into_one_turn() -> None:
    """360 이상은 접는다 — 폐기하면 한 바퀴 경계의 점을 매 스캔 잃는다."""
    points = points_from_wire([[360.0, 1000], [450.0, 1000]])
    assert points[0][0] == pytest.approx(0.0, abs=1e-9)
    assert points[1][0] == pytest.approx(1.5707963, abs=1e-6)


def test_encode_round_trips_through_the_decoder() -> None:
    """중계 노드 참조 구현이 자기 디코더를 통과한다."""
    line = encode_scan(
        seq=7,
        ts_ms=1756800000000,
        device_id="lidar-a",
        boot_id="7f3a91c2e8b40d65",
        points_wire=[[0.0, 1200], [180.0, 900]],
    )
    assert "\n" not in line, "한 줄 JSON 이어야 UDP·JSONL 양쪽에서 안전하다"
    scan = scan_of(ScanDecoder().decode(line))
    assert isinstance(scan, Scan)
    assert scan.seq == 7
    assert scan.scan_id == 7, "seq 가 곧 Scan ID 다"
    assert len(scan.points) == 2
    assert scan.dropped == 0
