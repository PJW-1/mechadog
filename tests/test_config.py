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
    "escalation",
    "auth",
    "posture",
    "change_detect",
    "zones",
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
    """측위 트랙은 docs/DECISIONS.md ADR-18 이 인정하는 값이어야 한다.

    `phone_vio`(Track B)는 **탈락했으므로 허용하지 않는다** (OI-9 닫힘, 2026-09-05).
    탈락한 선택지를 설정에 남겨 두면 근거를 모르는 사람이 다시 넣는다.
    """
    assert cfg["localization"]["track"] in {"none", "lidar", "aruco"}


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


def test_escalation_led_covers_all_levels(cfg: dict) -> None:
    """L0~L3 과 페일세이프의 색상이 모두 정의되어 있어야 한다 (아키텍처 3.1).

    색상이 빠지면 그 단계에서 로봇 상태를 읽을 수 없다.
    """
    led = cfg["escalation"]["led"]
    for key in ("l0_patrol", "l1_observe", "l2_auth_request", "l3_alarm", "failsafe"):
        assert key in led, f"에스컬레이션 색상 누락: {key}"
        assert isinstance(led[key], str)


def test_escalation_l3_requires_manual_reset(cfg: dict) -> None:
    """L3·페일세이프는 자동 해제하지 않는다 (아키텍처 3.1 해제 규칙)."""
    assert cfg["escalation"]["l3_requires_manual_reset"] is True


def test_auth_bound_to_track_id(cfg: dict) -> None:
    """인증은 추적 ID 에 귀속되어야 한다 (FR-3.6.2).

    아니면 인원이 여러 명일 때 누가 인증되었는지 구분할 수 없다.
    """
    assert cfg["auth"]["bind_to_track_id"] is True


def test_auth_timeouts_are_ordered(cfg: dict) -> None:
    """인증 유효시간이 시도 타임아웃보다 길어야 한다 (FR-10.2.4 / 10.3)."""
    auth = cfg["auth"]
    assert auth["session_valid_s"] > auth["timeout_s"]
    assert auth["max_attempts"] >= 1


def test_posture_returns_before_move(cfg: dict) -> None:
    """상향 자세에서는 지면이 안 보이므로 이동 전 복귀해야 한다 (FR-9.2.3)."""
    assert cfg["posture"]["return_before_move"] is True


def test_posture_steps_are_known(cfg: dict) -> None:
    """자세 상승 단계는 정의된 수단만 사용한다 (FR-9.2.2).

    stand_two_legs 는 측위 센서 장착 시 전도 위험으로 제외한다.
    """
    steps = cfg["posture"]["escalation_steps"]
    assert steps, "자세 상승 단계가 비어 있다"
    assert set(steps) <= {"pitch_up", "sit", "back_off"}
    assert "stand_two_legs" not in steps


def test_change_detect_confirms_over_cycles(cfg: dict) -> None:
    """물체 변화는 연속 사이클 확인 후 확정한다 (FR-8.4).

    단 person 출현은 즉시 처리한다.
    """
    cd = cfg["change_detect"]
    assert cd["confirm_cycles"] >= 2
    assert cd["person_immediate"] is True


def test_zones_are_declared(cfg: dict) -> None:
    """순찰 구역 A~E 가 선언되어 있어야 한다 (FR-7.1)."""
    zones = cfg["zones"]
    assert len(zones["ids"]) >= 2
    assert zones["arrival_radius_mm"] > 0


def test_reconnect_backoff_is_increasing(cfg: dict) -> None:
    """지수 백오프는 단조 증가해야 한다 (FR-5.3)."""
    backoff = cfg["vision"]["reconnect_backoff_s"]
    assert len(backoff) >= 2
    assert backoff == sorted(backoff)


# ── PPE 판정 조건 — 거리 기반 파라미터 재유입 방지 ──────────────

FORBIDDEN_PPE_KEYS = (
    "min_distance_m",  # DR-15 — 미터 거리를 구하지 않는다
    "max_distance_m",
    "require_pitch_up",  # FR-9.2.1 — 자세 상승은 판정 전제가 아니다
)


@pytest.mark.parametrize("key", FORBIDDEN_PPE_KEYS)
def test_ppe_has_no_distance_based_keys(cfg: dict, key: str) -> None:
    """PPE 판정에 거리 기반 파라미터를 두지 않는다 (DR-15).

    거리를 측정할 수단이 없다 — 깊이 모델과 초음파를 모두 배제했고, 초음파는
    정면 근거리만 본다. 구현할 수 없는 파라미터가 설정에 남아 있으면
    PRD 와 충돌하고, 구현자가 어느 쪽을 따를지 알 수 없다.

    `require_pitch_up` 도 금지한다. 자세 상승은 판정의 전제가 아니라
    bbox 클리핑 시의 대응 수단이며(FR-9.2.2), 단계는 posture 절이 정의한다.
    """
    assert key not in cfg["vision"]["ppe"], (
        f"vision.ppe.{key} 는 DR-15/FR-9.2.1 과 충돌한다. "
        "판정은 require_head_visible(bbox 클리핑)으로 한다"
    )


def test_ppe_uses_bbox_clipping_condition(cfg: dict) -> None:
    """판정 조건은 bbox 상단 클리핑 여부다 (FR-9.2.1)."""
    ppe = cfg["vision"]["ppe"]
    assert ppe.get("require_head_visible") is True, "require_head_visible 이 없거나 꺼져 있다"
    assert ppe.get("head_margin_px", 0) > 0, "head_margin_px 가 없거나 0 이다"


# ── 자세 상승 루프 방지 (FR-9.2.0 / FR-9.2.4) ──────────────────


def test_ppe_requires_static_target(cfg: dict) -> None:
    """자세 상승은 대상이 정지 상태일 때만 개시한다 (FR-9.2.0).

    이동하는 대상은 추종이 불가능하다 — MechDog Trot 약 10~30cm/s 대
    사람 보행 120~150cm/s 로 5~15배 차이이고, 제자리 회전도 불가하다(DR-11).
    게다가 상향 자세에서는 이동할 수 없으므로(FR-9.2.3) 대상이 움직이면
    `자세 상승 → 이탈 → 복귀 → 이동 → 재클리핑` 루프에 빠진다.
    """
    ppe = cfg["vision"]["ppe"]
    assert ppe.get("require_target_static") is True, "require_target_static 이 없거나 꺼져 있다"
    assert ppe.get("static_threshold_px", 0) > 0
    assert ppe.get("static_frames", 0) >= 2, "단발 프레임으로 정지를 판정하면 안 된다"


def test_posture_escalation_has_retry_limit(cfg: dict) -> None:
    """자세 상승 재시도에 상한이 있어야 한다 (FR-9.2.4).

    상한이 없으면 대상이 계속 움직일 때 로봇이 자세만 오르내리며 순찰로
    돌아가지 못한다. 관측자에게는 고장으로 보인다.
    """
    retries = cfg["vision"]["ppe"].get("max_posture_retries")
    assert retries is not None, "max_posture_retries 가 없다 — 루프 상한이 없다"
    assert 1 <= retries <= 5, f"재시도 {retries}회는 비현실적이다"


def test_posture_aborts_on_target_lost(cfg: dict) -> None:
    """자세 상승 중 대상이 이탈하면 즉시 중단해야 한다 (FR-9.2.4)."""
    assert cfg["posture"].get("abort_on_target_lost") is True


# ── 다중 대상 처리 (FR-3.8) ────────────────────────────────────


def test_multi_target_policy_is_max(cfg: dict) -> None:
    """미인증 인원이 있으면 에스컬레이션을 유지한다 (FR-3.8.1).

    인증 상태는 마커·암구호 이벤트로 결정되며 주 대상 여부와 무관하게
    대상별로 집계된다. 최댓값을 취하는 것이 안전측이다.
    """
    assert cfg["escalation"].get("multi_target_policy") == "max"


def test_primary_target_criterion_is_not_judgment_based(cfg: dict) -> None:
    """주 대상 선정 기준은 판정 결과가 아니어야 한다 (FR-3.8.2).

    판정 결과(에스컬레이션 단계)를 기준으로 쓰면 순환 논리가 된다 —
    단계는 판정으로 올라가고, 판정은 주 대상에게만 수행하므로 비주 대상의
    단계는 영원히 올라가지 않는다.

    bbox 높이는 판정과 무관하게 매 프레임 측정되므로 순환이 없다.
    """
    criterion = cfg["vision"].get("primary_target_criterion")
    assert criterion == "nearest", (
        f"주 대상 기준이 '{criterion}' 이다. 판정 결과 기반 기준은 순환 논리다 — "
        "'nearest'(bbox 최대)를 쓴다"
    )


def test_primary_target_has_hold_time(cfg: dict) -> None:
    """주 대상 전환에 히스테리시스가 있어야 한다 (FR-3.8.3)."""
    hold = cfg["vision"].get("primary_target_hold_ms")
    assert hold is not None, "primary_target_hold_ms 가 없다"
    assert hold >= 1000, f"{hold}ms 는 너무 짧다 — 자세 시퀀스가 완결되지 않는다"


def test_primary_target_sequence_lock(cfg: dict) -> None:
    """자세 상승·인증 시퀀스 진행 중에는 주 대상을 바꾸지 않는다 (FR-3.8.3).

    히스테리시스 시간이 지나도 전환하면 안 된다. 자세를 올리는 중에 주 대상이
    바뀌면 그 시퀀스 자체가 무의미해지므로, 시퀀스 락이 히스테리시스보다 강하다.
    """
    assert cfg["vision"].get("primary_target_seq_lock") is True


def test_tracked_persons_has_upper_bound(cfg: dict) -> None:
    """동시 추적 인원에 상한이 있어야 한다 (FR-3.8.5)."""
    n = cfg["vision"].get("max_tracked_persons")
    assert n is not None, "max_tracked_persons 가 없다"
    assert 2 <= n <= 20, f"상한 {n}명은 비현실적이다"
