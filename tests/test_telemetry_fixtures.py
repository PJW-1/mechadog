"""텔레메트리 정본 픽스처 검증 (FR-5.2).

제어 명령과 마찬가지로, 텔레메트리도 **보내는 쪽과 받는 쪽을 다른 사람이
작성한다** — 펌웨어는 L1·L2, 호스트 수신은 팀장이다(WBS 담당자 기준).
불일치하면 대시보드가 조용히 빈 값을 표시하거나, 안전 플래그를 놓친다.

그래서 제어 명령(`test_protocol_fixtures.py`)과 동일한 방식으로 정본 픽스처를 둔다.

  · tests/fixtures/telemetry_samples.jsonl  — 유효 레코드 정본
  · tests/fixtures/telemetry_invalid.jsonl  — 폐기 대상 + 기대 동작

이 파일의 핵심은 단순 스키마 검사가 아니라 **config 임계값과의 교차 검증**이다.
픽스처의 경계값이 `config.yaml` 의 안전 임계와 어긋나면, 그 임계를 넘나드는
동작이 실제로는 한 번도 검증되지 않는다.

WBS 4.1.4(텔레메트리 송신)·4.3.x(호스트 수신) 구현 시 이 픽스처를 기준으로
왕복 검증을 추가한다.
"""

import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLES = FIXTURES / "telemetry_samples.jsonl"
INVALID = FIXTURES / "telemetry_invalid.jsonl"
CONFIG = ROOT / "config" / "config.yaml"

# PRD FR-2.1 ~ FR-6.7 — FSM 상태
KNOWN_STATES = {
    "PATROL",
    "SCAN",
    "AVOID",
    "ALERT",
    "TRACK",
    "LOST",
    "FAILSAFE",
    "HAZARD_DISPATCH",
}

# FR-5.2 — 최상위 필수 필드
REQUIRED_TOP = ("seq", "ts", "device_id", "state", "dist_cm", "imu", "batt_v", "flags")
REQUIRED_IMU = ("pitch", "roll", "yaw")
REQUIRED_FLAGS = ("lowbatt", "tipped", "link_ok")

# 2S 리튬 물리 범위 — 셀당 3.0~4.2V
BATT_MIN, BATT_MAX = 6.0, 8.4


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


@pytest.fixture(scope="module")
def cfg() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


# ── 스키마 ──────────────────────────────────────────────────────


def test_samples_have_required_fields(samples: list[dict]) -> None:
    for m in samples:
        missing = [k for k in REQUIRED_TOP if k not in m]
        assert not missing, f"{m['_case']}: 필수 필드 누락 {missing}"


def test_imu_and_flags_are_complete(samples: list[dict]) -> None:
    """imu·flags 는 중첩 객체이므로 하위 키까지 확인한다."""
    for m in samples:
        for key in REQUIRED_IMU:
            assert key in m["imu"], f"{m['_case']}: imu.{key} 누락"
        for key in REQUIRED_FLAGS:
            assert key in m["flags"], f"{m['_case']}: flags.{key} 누락"


def test_device_id_is_present_and_distinct(samples: list[dict]) -> None:
    """다중 개체를 구분할 수 있어야 한다 (DR-17).

    NAT 를 거치면 송신자 IP 가 게이트웨이로 보이므로, device_id 가 유일한
    구분 수단이다. 픽스처에 개체가 둘 이상 없으면 그 경로가 검증되지 않는다.
    """
    ids = {m["device_id"] for m in samples}
    assert len(ids) >= 2, f"픽스처에 개체가 하나뿐이다: {ids}"


def test_states_are_known(samples: list[dict]) -> None:
    for m in samples:
        assert m["state"] in KNOWN_STATES, f"{m['_case']}: 미지 상태 {m['state']}"


def test_samples_cover_every_state(samples: list[dict]) -> None:
    """FSM 상태 전체가 최소 1건씩 나타나야 한다.

    상태를 추가하면서 픽스처를 빠뜨리면 대시보드가 그 상태를 표시하지 못하는
    것을 통합 단계에서야 발견한다.
    """
    covered = {m["state"] for m in samples}
    missing = KNOWN_STATES - covered
    assert not missing, f"픽스처에 없는 상태: {sorted(missing)}"


def test_seq_is_monotonic(samples: list[dict]) -> None:
    seqs = [m["seq"] for m in samples]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs)), "seq 중복"


def test_battery_within_physical_range(samples: list[dict]) -> None:
    for m in samples:
        assert BATT_MIN <= m["batt_v"] <= BATT_MAX, (
            f"{m['_case']}: batt_v={m['batt_v']} 물리 범위 이탈"
        )


def test_distance_is_non_negative(samples: list[dict]) -> None:
    for m in samples:
        assert m["dist_cm"] >= 0, f"{m['_case']}: dist_cm 음수"


def test_yaw_within_circle(samples: list[dict]) -> None:
    for m in samples:
        assert 0.0 <= m["imu"]["yaw"] < 360.0, f"{m['_case']}: yaw 범위 이탈"


# ── config 교차 검증 — 이 파일의 핵심 ──────────────────────────


def test_battery_thresholds_have_boundary_samples(samples: list[dict], cfg: dict) -> None:
    """저전압 경고·셧다운 임계값이 픽스처에 정확히 등장해야 한다.

    임계를 스치는 레코드가 없으면 경계 판정이 검증되지 않는다.
    config 를 바꾸면 픽스처도 함께 바꾸도록 강제한다.
    """
    volts = {m["batt_v"] for m in samples}
    for key in ("battery_warn_v", "battery_shutdown_v"):
        v = cfg["safety"][key]
        assert v in volts, f"safety.{key}={v} 경계 샘플이 픽스처에 없다"


def test_lowbatt_flag_agrees_with_threshold(samples: list[dict], cfg: dict) -> None:
    """batt_v 가 경고 임계 이하이면 lowbatt 플래그가 서 있어야 한다."""
    warn = cfg["safety"]["battery_warn_v"]
    for m in samples:
        if m["batt_v"] <= warn:
            assert m["flags"]["lowbatt"] is True, f"{m['_case']}: lowbatt 가 서 있지 않다"


def test_tip_threshold_separates_posture_from_fall(samples: list[dict], cfg: dict) -> None:
    """전도 임계 경계와 '의도된 자세' 샘플이 모두 있어야 한다 (DR-16).

    앉기 자세(최대 45°)는 tipped 가 아니고, 임계 이상은 tipped 여야 한다.
    이 두 케이스가 함께 있어야 각도만으로 분리하는 설계가 검증된다.
    """
    tip = cfg["safety"]["tip_angle_deg"]
    at_threshold = [m for m in samples if abs(m["imu"]["pitch"]) >= tip]
    intended = [m for m in samples if 30 <= abs(m["imu"]["pitch"]) < tip]
    assert at_threshold, f"tip_angle_deg={tip} 이상인 샘플이 없다"
    assert intended, "의도된 자세(30° 이상, 임계 미만) 샘플이 없다 — DR-16 검증 불가"
    for m in at_threshold:
        assert m["flags"]["tipped"] is True, f"{m['_case']}: tipped 가 서 있지 않다"
    for m in intended:
        assert m["flags"]["tipped"] is False, f"{m['_case']}: 의도된 자세인데 tipped"


def test_obstacle_stop_boundary_exists(samples: list[dict], cfg: dict) -> None:
    """초음파 반사 정지 임계값 경계 샘플이 있어야 한다 (FR-2.2)."""
    stop = cfg["safety"]["obstacle_stop_cm"]
    dists = {m["dist_cm"] for m in samples}
    assert stop in dists, f"safety.obstacle_stop_cm={stop} 경계 샘플이 없다"


def test_link_timeout_boundaries_exist(samples: list[dict], cfg: dict) -> None:
    """명령 타임아웃과 링크 두절 페일세이프 경계가 모두 있어야 한다."""
    ages = {m["last_cmd_age_ms"] for m in samples}
    for key in ("cmd_timeout_ms", "link_loss_failsafe_ms"):
        v = cfg["safety"][key]
        assert v in ages, f"safety.{key}={v} 경계 샘플이 없다"


def test_failsafe_state_accompanies_a_cause(samples: list[dict], cfg: dict) -> None:
    """FAILSAFE 레코드에는 원인이 드러나야 한다.

    저전압·전도·링크두절 중 하나가 보이지 않으면 대시보드가 이유를 표시할 수 없다.
    """
    safety = cfg["safety"]
    for m in samples:
        if m["state"] != "FAILSAFE":
            continue
        cause = (
            m["batt_v"] <= safety["battery_shutdown_v"]
            or m["flags"]["tipped"]
            or not m["flags"]["link_ok"]
            or m["last_cmd_age_ms"] >= safety["link_loss_failsafe_ms"]
        )
        assert cause, f"{m['_case']}: FAILSAFE 인데 원인이 드러나지 않는다"


# ── 폐기 픽스처 ────────────────────────────────────────────────


def test_invalid_fixture_declares_expected_handling(invalid: list[dict]) -> None:
    allowed = {"discard", "discard_warn"}
    for m in invalid:
        assert m.get("_expect") in allowed, f"{m.get('_case')}: _expect 값이 잘못됨"


def test_unknown_state_case_exists(invalid: list[dict]) -> None:
    """미지 상태 폐기 케이스가 있어야 한다.

    제어 명령의 '미지 type 폐기'(FR-5.1 규칙 ④)와 대칭이며, 상태 추가를
    하위 호환으로 만들어 준다.
    """
    unknown = [m for m in invalid if m.get("state") and m["state"] not in KNOWN_STATES]
    assert unknown, "미지 상태 케이스 누락"
    for m in unknown:
        assert m["_expect"] == "discard_warn"


def test_device_id_missing_case_exists(invalid: list[dict]) -> None:
    """device_id 누락 폐기 케이스가 있어야 한다 (DR-17)."""
    assert any("device_id" not in m for m in invalid), "device_id 누락 케이스가 없다"
