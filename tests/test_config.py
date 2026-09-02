"""config.yaml 스키마 검증.

NFR-3① (파라미터화)의 최소 안전망이다. 코드가 참조하는 키가 설정에서
사라지면 런타임이 아니라 CI에서 잡히게 한다.

WBS 4.4.1 의 config 로더가 구현되면 이 테스트를 로더 기반으로 확장한다.
"""

from pathlib import Path

import pytest
import yaml

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "config.yaml"

# 코드가 의존하는 키 — 안전 임계값은 Tier 1 판정에 직결되므로 누락을 허용하지 않는다
REQUIRED_SECTIONS = (
    "network",
    "safety",
    "gait",
    "fsm",
    "vision",
    "localization",
    "logging",
)

REQUIRED_SAFETY_KEYS = (
    "cmd_timeout_ms",
    "link_loss_failsafe_ms",
    "obstacle_stop_cm",
    "battery_warn_v",
    "battery_shutdown_v",
    "tip_angle_deg",
    "tip_duration_ms",
)


@pytest.fixture(scope="module")
def cfg() -> dict:
    assert CONFIG_PATH.exists(), f"설정 파일 없음: {CONFIG_PATH}"
    with CONFIG_PATH.open(encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    assert isinstance(loaded, dict), "config.yaml 최상위는 매핑이어야 한다"
    return loaded


def test_profile_is_valid(cfg: dict) -> None:
    assert cfg.get("profile") in {"dev", "prod"}


@pytest.mark.parametrize("section", REQUIRED_SECTIONS)
def test_required_sections_exist(cfg: dict, section: str) -> None:
    assert section in cfg, f"필수 섹션 누락: {section}"


@pytest.mark.parametrize("key", REQUIRED_SAFETY_KEYS)
def test_required_safety_keys_exist(cfg: dict, key: str) -> None:
    assert key in cfg["safety"], f"안전 파라미터 누락: safety.{key}"


def test_battery_thresholds_ordered(cfg: dict) -> None:
    """저전압 셧다운은 경고보다 낮아야 한다 (NFR-2.3).

    2S 리튬 기준. 순서가 뒤바뀌면 셧다운이 먼저 걸려 로봇이 기동하지 못한다.
    """
    safety = cfg["safety"]
    assert safety["battery_shutdown_v"] < safety["battery_warn_v"]
    # 셀당 3.3V(=6.6V) 미만은 과방전 영역이므로 하한을 둔다
    assert safety["battery_shutdown_v"] >= 6.6


def test_command_timeout_shorter_than_link_loss(cfg: dict) -> None:
    """정지(FR-1.3)가 페일세이프(FR-1.5)보다 먼저 발동해야 한다."""
    safety = cfg["safety"]
    assert safety["cmd_timeout_ms"] < safety["link_loss_failsafe_ms"]


def test_command_timeout_within_reflex_budget(cfg: dict) -> None:
    """명령 타임아웃은 Tier 1 예산(FR-1.3 = 300ms) 이내여야 한다."""
    assert 0 < cfg["safety"]["cmd_timeout_ms"] <= 300


def test_gait_params_within_api_range(cfg: dict) -> None:
    """HW_MechDog API 허용 범위 (DR-1).

    move(step_length, angle) — step -100~100mm, angle -30~30deg.
    범위를 벗어난 값은 라이브러리가 어떻게 처리할지 보장되지 않는다.
    """
    gait = cfg["gait"]
    assert -100 <= gait["step_length_mm"] <= 100
    assert -30 <= gait["turn_angle_deg"] <= 30


def test_localization_track_is_known(cfg: dict) -> None:
    """측위 트랙은 docs/LOCALIZATION_OPTIONS.md 의 후보 중 하나여야 한다."""
    assert cfg["localization"]["track"] in {"none", "lidar", "phone_vio", "aruco"}


def test_detection_requires_consecutive_frames(cfg: dict) -> None:
    """단발 오검출로 ALERT 로 튀지 않도록 2프레임 이상을 요구한다 (FR-3.2)."""
    assert cfg["vision"]["detect_consecutive_frames"] >= 2


def test_tip_angle_separates_posture_from_fall(cfg: dict) -> None:
    """전도 임계는 의도된 자세와 실제 전도 사이에 있어야 한다 (DR-16).

    앉기 자세의 몸통 피치는 30~45°에 달한다. 임계를 그 아래로 두면
    앉을 때마다 페일세이프가 발동해 기능이 성립하지 않고, 결국 팀이
    전도 감지를 꺼버려 서보 보호가 완전히 사라진다.
    """
    tip = cfg["safety"]["tip_angle_deg"]
    assert tip >= 55, "의도된 자세(최대 45°)와 겹친다 — DR-16 위반"
    assert tip <= 80, "실제 전도(약 90°)를 놓칠 수 있다"


def test_two_stage_detector_declared(cfg: dict) -> None:
    """COCO 범용 + PPE 전용 2단 구조가 선언되어 있어야 한다 (DR-12)."""
    vision = cfg["vision"]
    assert "coco" in vision, "COCO 범용 검출기 설정 누락"
    assert "ppe" in vision, "PPE 전용 모델 설정 누락"


def test_ppe_violation_classes_are_subset(cfg: dict) -> None:
    """위반 클래스는 학습 클래스의 부분집합이어야 한다.

    오타나 클래스명 변경 시 위반 판정이 조용히 동작하지 않는 것을 막는다.
    """
    ppe = cfg["vision"]["ppe"]
    assert set(ppe["violation_classes"]) <= set(ppe["classes"])


def test_ppe_excludes_person_class(cfg: dict) -> None:
    """person 은 COCO 검출기가 담당한다. PPE 모델에서 중복 학습하지 않는다 (FR-9.1.1)."""
    assert "person" not in cfg["vision"]["ppe"]["classes"]


def test_providers_fallback_ends_with_cpu(cfg: dict) -> None:
    """EP 목록의 마지막은 CPU 여야 한다.

    GPU 를 못 쓰는 팀원 PC 나 CI 러너에서도 동작해야 한다 (DR-13).
    """
    providers = cfg["vision"]["providers"]
    assert providers[-1] == "CPUExecutionProvider"


def test_inference_fps_not_above_stream_fps(cfg: dict) -> None:
    """추론 주기가 스트림 fps 를 넘을 수 없다."""
    vision = cfg["vision"]
    assert 0 < vision["inference_fps"] <= vision["target_fps"]


def test_reconnect_backoff_is_increasing(cfg: dict) -> None:
    """지수 백오프는 단조 증가해야 한다 (FR-5.3)."""
    backoff = cfg["vision"]["reconnect_backoff_s"]
    assert len(backoff) >= 2
    assert backoff == sorted(backoff)
