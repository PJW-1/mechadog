"""전역 설정과 MechDog 개체 프로파일을 읽고 시작 전에 검증한다."""

from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "config.yaml"
DEFAULT_DEVICES_DIR = ROOT / "config" / "devices"

REQUIRED_SECTIONS = (
    "network",
    "safety",
    "gait",
    "mission",
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


class ConfigError(ValueError):
    """설정 누락이나 범위 오류 때문에 안전하게 기동할 수 없음."""


def repo_path(value: str | Path) -> Path:
    """설정의 상대 경로를 **저장소 루트 기준**으로 푼다. 절대 경로는 그대로 둔다.

    실행 위치(CWD) 기준으로 두면 저장소 밖에서 띄웠을 때 모델을 못 찾고, 로그와
    블랙박스가 띄운 자리마다 흩어진다.
    """
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def telemetry_ids(config: dict[str, Any], device_id: str) -> frozenset[str]:
    """이 개체의 텔레메트리로 받아들이는 `device_id` 들.

    ⚠️ **펌웨어는 설정 이름이 아니라 보드 MAC 으로 만든 이름을 보낸다**
    (`mechdog-<MAC 12자리>` · `telemetry_publisher.cpp`). 그래서 설정 이름(`mechdog-01`)으로만
    대조하면 우리 로봇의 텔레메트리를 전부 남의 것으로 버린다 — 2026-09-12 실기에서 그랬다.
    개체 프로파일의 `telemetry_device_id` 로 잇고, 목업은 설정 이름을 그대로 보내므로 둘 다 받는다.
    """
    named = config.get("telemetry_device_id")
    return frozenset({device_id, named}) if named else frozenset({device_id})


def _read_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"설정 파일 없음: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 파싱 실패: {path}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"설정 최상위는 매핑이어야 함: {path}")
    return loaded


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def _finite_number(value: Any) -> bool:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _require_positive(mapping: dict[str, Any], key: str) -> None:
    value = mapping.get(key)
    if not _finite_number(value) or value <= 0:
        raise ConfigError(f"{key} 는 0보다 큰 유한한 수여야 함")


def validate_base_config(config: dict[str, Any]) -> None:
    if not isinstance(config.get("profile"), str) or config["profile"] not in {"dev", "prod"}:
        raise ConfigError("profile 은 dev 또는 prod 여야 함")
    missing = [
        section for section in REQUIRED_SECTIONS if not isinstance(config.get(section), dict)
    ]
    if missing:
        raise ConfigError(f"필수 설정 섹션 누락: {missing}")

    blackbox_dir = config["logging"].get("blackbox_dir")
    if not isinstance(blackbox_dir, str) or not blackbox_dir.strip():
        raise ConfigError("logging.blackbox_dir 는 비어 있지 않은 문자열이어야 함")

    # ⚠️ **이름 목록을 여기 적지 않는다** (FR-11.1). 고를 수 있는 모드와 그 선행
    # 기능의 정본은 `behavior/mission.py` 하나이며, 목록을 두 곳에 두면 모드를
    # 늘릴 때 한쪽만 고쳐진다 — `coco_labels` 를 `config.yaml` 에 두지 않은 것과
    # 같은 이유다. 여기서는 **자리가 있고 값이 문자열인지**까지만 본다.
    mode = config["mission"].get("mode")
    if not isinstance(mode, str) or not mode.strip():
        raise ConfigError("mission.mode 는 비어 있지 않은 문자열이어야 함")

    network = config["network"]
    for name in ("cmd_port", "telemetry_port", "vision_control_port", "vision_stream_port"):
        port = network.get(name)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ConfigError(f"network.{name} 는 1~65535 정수여야 함")
    for name in ("cmd_rate_hz", "telemetry_rate_hz"):
        _require_positive(network, name)
    # 상한이 없으면 최악 부하를 계산할 수 없다 (config 주석 참고).
    vision = config["vision"]
    _require_positive(vision, "stream_fps_limit")
    _require_positive(vision, "stall_timeout_ms")
    _require_positive(vision, "target_fps")
    _require_positive(vision, "inference_fps")
    # 장착 방향 보정은 0 또는 180 뿐이다 — 펌웨어가 vflip+hmirror 합성으로 구현하므로
    # 90·270 은 만들 수 없다. 여기서 막지 않으면 카메라가 400 을 돌려주고 그것을
    # 기동 경고로만 보게 된다.
    if vision.get("mount_rotation", 0) not in (0, 180):
        raise ConfigError("vision.mount_rotation 은 0 또는 180 이어야 함")
    # ⚠️ **추론률의 상한은 `target_fps` 가 아니라 `stream_fps_limit` 이다.**
    # `target_fps` 는 NFR-1.3 이 요구하는 *하한*(≥15fps)이고 실제 수신률은 상한값이다.
    # 하한을 상한으로 쓰면 25fps 를 받는데도 추론률을 15 위로 못 올린다 — 지키려던
    # 불변식("낡은 프레임으로 판단하지 않는다")과 무관한 제약이 된다.
    if vision["inference_fps"] > vision["stream_fps_limit"]:
        raise ConfigError("vision.inference_fps 는 vision.stream_fps_limit 을 넘을 수 없음")
    if vision["stream_fps_limit"] < vision["target_fps"]:
        raise ConfigError("vision.stream_fps_limit 이 NFR-1.3 하한(target_fps) 미달")
    # 검출기 절 — 값이 음수·0 이면 추론이 조용히 이상해진다(빈 결과, 격자 불일치).
    for section in ("coco", "ppe"):
        spec = vision.get(section)
        if not isinstance(spec, dict):
            raise ConfigError(f"vision.{section} 절이 없음")
        _require_positive(spec, "input_size")
        _require_positive(spec, "conf_threshold")
        family = spec.get("model_family")
        # `null` 은 "아직 정하지 않았다" 는 뜻으로 허용한다. 빈 문자열은 실수다.
        if family is not None and (not isinstance(family, str) or not family.strip()):
            raise ConfigError(f"vision.{section}.model_family 는 null 이거나 비어 있지 않은 문자열")

    # 사람 판정 (FR-3.2) — **시간 기반이다.** 프레임 수로 두면 추론률에 종속된다.
    _require_positive(vision, "detect_window_ms")
    _require_positive(vision, "detect_hits_required")
    for name in ("detect_window_ms", "detect_hits_required"):
        if not isinstance(vision[name], int) or isinstance(vision[name], bool):
            raise ConfigError(f"vision.{name} 는 양의 정수여야 함")
    # ⚠️ 창 안에 그만큼의 관측이 들어갈 수 없으면 **영원히 확정되지 않는다.**
    # 게이트가 양 끝을 포함하므로 최대 개수는 `floor(window / period) + 1` 이다.
    period_ms = max(1, round(1000 / vision["inference_fps"]))
    capacity = vision["detect_window_ms"] // period_ms + 1
    if vision["detect_hits_required"] > capacity:
        raise ConfigError(
            f"vision.detect_hits_required({vision['detect_hits_required']}) 가 "
            f"창 안 최대 관측 수({capacity} = floor({vision['detect_window_ms']}ms / "
            f"{period_ms}ms) + 1) 를 넘어 영원히 확정되지 않음"
        )

    # 다중 인원 추적 (FR-3.6) — 소실 버퍼도 **시간이다.** 프레임 수로 두면
    # 같은 30프레임이 10fps 3초 · 25fps 1.2초가 된다 (결정 22·25번과 같은 형태).
    tracker = vision.get("tracker")
    if not isinstance(tracker, dict):
        raise ConfigError("vision.tracker 절이 없음")
    _require_positive(tracker, "iou_match_threshold")
    _require_positive(tracker, "track_lost_ms")
    _require_positive(vision, "max_tracked_persons")
    if tracker["iou_match_threshold"] >= 1:
        # 1.0 은 완전히 같은 박스만 잇는다는 뜻이라 어떤 대상도 이어지지 않는다.
        raise ConfigError("vision.tracker.iou_match_threshold 는 1 미만이어야 함")
    # ⚠️ 소실 버퍼가 추론 주기보다 짧으면 **한 번만 놓쳐도 ID 가 바뀐다.** 실기
    # 통과율이 52% 였으므로(ADR-25) 한 프레임 공백은 예외가 아니라 일상이다.
    if tracker["track_lost_ms"] < period_ms:
        raise ConfigError(
            f"vision.tracker.track_lost_ms({tracker['track_lost_ms']}ms) 가 추론 주기"
            f"({period_ms}ms) 보다 짧아 한 번만 놓쳐도 ID 가 바뀜"
        )

    # 인증 (FR-10) — 사원증 사전과 발급 대장.
    # 절의 존재는 `REQUIRED_SECTIONS` 가 이미 본다 — 여기서 또 보면 죽은 코드가 된다.
    auth = config["auth"]
    dictionary = auth.get("badge_dictionary")
    # 이름이 `cv2.aruco` 에 있는지는 `BadgeReader` 가 기동 때 확인한다 — 여기서
    # `cv2` 를 import 하면 설정 검증이 OpenCV 를 요구하게 된다.
    if not isinstance(dictionary, str) or not dictionary.strip():
        raise ConfigError("auth.badge_dictionary 는 비어 있지 않은 문자열이어야 함")
    _require_positive(auth, "session_valid_s")
    _require_positive(auth, "max_attempts")
    _require_positive(auth, "timeout_s")
    _require_positive(auth, "verdict_grace_s")
    _require_positive(auth, "unknown_marker_min_frames")
    if int(auth.get("resume_delay_ms", 3500)) < 0:
        raise ConfigError("auth.resume_delay_ms 는 0 이상이어야 함")
    if not isinstance(auth.get("require_both", False), bool):
        raise ConfigError("auth.require_both 는 true 또는 false 여야 함")
    badges = auth.get("badge_marker_map")
    if badges is None or not isinstance(badges, dict):
        raise ConfigError("auth.badge_marker_map 은 사전(dict)이어야 함")
    for key, value in badges.items():
        if not isinstance(key, int) or isinstance(key, bool):
            raise ConfigError(f"auth.badge_marker_map 키는 ArUco ID(정수)여야 함: {key!r}")
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"auth.badge_marker_map[{key}] 는 비어 있지 않은 문자열이어야 함")

    backoff = vision.get("reconnect_backoff_s")
    if not isinstance(backoff, list) or not backoff:
        raise ConfigError("vision.reconnect_backoff_s 는 비어 있지 않은 목록이어야 함")
    if not all(_finite_number(step) and step > 0 for step in backoff):
        raise ConfigError("vision.reconnect_backoff_s 는 양수만 담아야 함")
    if list(backoff) != sorted(backoff):
        # 지수 백오프가 아니면 이름과 동작이 어긋난다 — 줄어드는 간격은 폭주가 된다.
        raise ConfigError("vision.reconnect_backoff_s 는 증가하는 순서여야 함")

    safety = config["safety"]
    missing_safety = [name for name in REQUIRED_SAFETY_KEYS if name not in safety]
    if missing_safety:
        raise ConfigError(f"필수 안전 설정 누락: {missing_safety}")
    for name in REQUIRED_SAFETY_KEYS:
        _require_positive(safety, name)
    if safety["cmd_timeout_ms"] > 600:
        raise ConfigError("safety.cmd_timeout_ms 는 600ms 이하여야 함 (ADR-39)")
    if safety["cmd_timeout_ms"] >= safety["link_loss_failsafe_ms"]:
        raise ConfigError("명령 정지가 링크 페일세이프보다 먼저 동작해야 함")
    if safety["battery_shutdown_v"] >= safety["battery_warn_v"]:
        raise ConfigError("배터리 셧다운 전압은 경고 전압보다 낮아야 함")
    if not 6.0 <= safety["battery_shutdown_v"] < safety["battery_warn_v"] <= 8.4:
        raise ConfigError("배터리 임계값은 2S 검증 범위 6.0~8.4V 안이어야 함")
    if not 0 < safety["tip_angle_deg"] < 90:
        raise ConfigError("전도 임계각은 0도 초과 90도 미만이어야 함")
    if 1000 / network["cmd_rate_hz"] >= safety["cmd_timeout_ms"]:
        raise ConfigError("명령 송신 주기는 명령 타임아웃보다 짧아야 함")

    # 운용 루프가 직접 읽는 타이머다. 빠진 키가 Runtime 생성 중 KeyError로
    # 터지기 전에 ConfigError로 설명한다.
    for section, names in {
        "fsm": (
            "patrol_scan_interval_s",
            "scan_duration_s",
            "target_lost_timeout_s",
            "avoid_attempts",
        ),
        "auth": ("timeout_s",),
        "escalation": ("l1_to_l2_hold_s",),
    }.items():
        for name in names:
            _require_positive(config[section], name)

    fsm = config["fsm"]
    deadzone = fsm.get("track_deadzone_px")
    if not _finite_number(deadzone) or deadzone < 0:
        raise ConfigError("fsm.track_deadzone_px 는 0 이상의 유한한 수여야 함")

    # 추종 지시를 이어 가는 상한은 **대상 상실 타이머보다 짧아야 한다.** 같거나 길면
    # 상한이 하는 일이 없어지고, 대상이 사라진 뒤에도 `TRACK` 이 끝날 때까지 낡은
    # 각도로 계속 돈다 — 이 값을 둔 이유가 바로 그것을 막는 것이다.
    lost_ms = float(config["fsm"]["target_lost_timeout_s"]) * 1000.0
    coast = fsm.get("track_coast_ms")
    if not _finite_number(coast) or coast <= 0:
        raise ConfigError("fsm.track_coast_ms 는 0 보다 큰 유한한 수여야 함")
    if coast >= lost_ms:
        raise ConfigError(
            f"fsm.track_coast_ms({coast}) 가 target_lost_timeout_s({lost_ms:.0f}ms) 이상이다"
            " — 상한이 없으면 대상이 사라져도 낡은 각도로 계속 돈다"
        )

    # ⚠️ **«고개를 드는» 자세각은 음수다** — 2026-09-15 실기로 확정했다
    # (`POSE pitch=+15` → IMU 17.4, 앞이 내려감 / `-15` → -11.6, 앞이 올라감).
    # PROTOCOL 2절과 config 주석이 그것을 적어 두었지만 **지키는 코드가 없었다.**
    #
    # 여기서 막는 이유 — 같은 실수가 이미 한 번 났다. `tools/teleop.py` 의 좌우가
    # 뒤바뀐 채 **시험이 그 버그를 굳혀 두고 있었다**(`2.2.3` 기록). 부호는 실측으로만
    # 알 수 있고 한번 틀리면 눈으로 보고서야 아는 종류라, 실측한 결론을 설정 검증에
    # 박아 둔다. 양수로 되돌리면 경계 자세가 **바닥을 보게 되고** 가까이 있는 사람의
    # 머리가 더 잘린다(FR-9.2.2 가 자세로 풀려던 것과 정반대).
    for section, name in (
        ("fsm", "alert_pitch_deg"),
        ("fsm", "scan_pitch_deg"),
        ("posture", "pitch_up_deg"),
    ):
        value = config[section].get(name)
        if not _finite_number(value):
            raise ConfigError(f"{section}.{name} 는 유한한 수여야 함")
        if value >= 0:
            raise ConfigError(
                f"{section}.{name}({value}) 가 0 이상이다 — 고개를 드는 자세는 음수다"
                " (양수 pitch 는 앞이 내려간다 · PROTOCOL 2절, 2026-09-15 실측)"
            )

    track = config["localization"].get("track")
    if not isinstance(track, str) or track not in {"none", "lidar", "aruco"}:
        raise ConfigError("localization.track 은 none, lidar, aruco 중 하나여야 함")
    ppe = config["vision"].get("ppe")
    if not isinstance(ppe, dict) or not ppe:
        raise ConfigError("vision.ppe 필수 설정 누락")
    for name in (
        "input_size",
        "head_margin_px",
        "static_threshold_px",
        "static_frames",
        "max_posture_retries",
    ):
        _require_positive(ppe, name)
    for name in ("input_size", "static_frames", "max_posture_retries"):
        if not isinstance(ppe[name], int) or isinstance(ppe[name], bool):
            raise ConfigError(f"vision.ppe.{name} 는 정수여야 함")
    confidence = ppe.get("conf_threshold")
    if not _finite_number(confidence) or not 0 < confidence <= 1:
        raise ConfigError("vision.ppe.conf_threshold 는 0 초과 1 이하여야 함")
    for name in ("require_head_visible", "require_target_static"):
        if ppe.get(name) is not True:
            raise ConfigError(f"vision.ppe.{name} 안전 판정 조건은 켜져 있어야 함")
    if ppe["static_frames"] < 2 or not 1 <= ppe["max_posture_retries"] <= 5:
        raise ConfigError("PPE 정지 판정은 2프레임 이상, 재시도는 1~5회여야 함")

    providers = config["vision"].get("providers")
    if not isinstance(providers, list) or not providers:
        raise ConfigError("vision.providers 가 비어 있음")
    if providers[-1] != "CPUExecutionProvider":
        raise ConfigError("vision.providers 마지막은 CPUExecutionProvider 여야 함")


def validate_device_config(config: dict[str, Any], device_id: str) -> None:
    if config.get("device_id") != device_id:
        raise ConfigError(
            f"개체 프로파일 device_id 불일치: 요청={device_id!r}, 파일={config.get('device_id')!r}"
        )
    named = config.get("telemetry_device_id")
    if named is not None and (not isinstance(named, str) or not named.strip()):
        raise ConfigError("telemetry_device_id 는 비어 있지 않은 문자열이어야 함")
    if not isinstance(config.get("reference_role"), str) or config["reference_role"] not in {
        "phase1",
        "phase2",
        "none",
    }:
        raise ConfigError("reference_role 은 phase1, phase2, none 중 하나여야 함")

    # ⚠️ **`null` 은 "아직 안 쟀다" 이며 0 아홉 개와 다르다.** 0 은 *"보정이
    # 필요 없다"* 는 뜻이 되고, 이 값은 유실 대비 보관본이라(호스트는 읽기만
    # 하고 로봇에 보내지 않는다) 틀린 기록이 그대로 남는다. 3대 중 아직
    # 안 잰 기체가 있으므로 비워 두는 길을 남긴다.
    offsets = config.get("servo_offset")
    if offsets is not None and (
        not isinstance(offsets, list)
        or len(offsets) != 9
        or not all(_finite_number(value) for value in offsets)
    ):
        raise ConfigError("servo_offset 은 유한한 수 9개이거나 null 이어야 함")

    network = config["network"]
    for name in ("cmd_port", "telemetry_port"):
        port = network.get(name)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ConfigError(f"network.{name} 는 1~65535 정수여야 함")

    calibration = config.get("gait_calibration")
    if isinstance(calibration, dict):
        _validate_posture_amplitude(calibration)

    if config["profile"] == "prod":
        if not isinstance(config.get("owner_id"), str) or config["owner_id"] in {
            "",
            "unassigned",
        }:
            raise ConfigError("prod 프로파일은 실제 owner_id 가 필요함")
        if not isinstance(calibration, dict):
            raise ConfigError("prod 프로파일은 보행 캘리브레이션 실측값이 필요함")
        for name in ("forward_mm_per_sec", "turn_deg_per_sec"):
            value = calibration.get(name)
            if not _finite_number(value) or value <= 0:
                raise ConfigError(f"prod gait_calibration.{name} 실측값이 필요함")
        if not isinstance(calibration.get("measured_on"), str) or not calibration["measured_on"]:
            raise ConfigError("prod gait_calibration.measured_on 기록이 필요함")


def _validate_posture_amplitude(calibration: dict[str, Any]) -> None:
    """트롯 보행 중 자세 진폭 (WBS 2.2.3 ②).

    ⚠️ **0 을 거부하는 것이 이 함수의 존재 이유다.** WBS 가 그 함정을 이미
    적어 두었다 — 자리만 만들어 두면 누군가 0 을 채우고 *"쟀다"* 로 보인다.
    진폭 0 은 로봇이 걷지 않았다는 뜻이므로 측정값일 수 없다.

    없으면 통과한다. 아직 재지 않은 기체가 있고(3대 중 1대만 끝났다) 없는 것과
    0 인 것은 다르다 — 없으면 `FR-6.2.2` 판단을 미루면 되지만 0 이면 **흔들리지
    않는다고 잘못 읽는다.**
    """
    amplitude = calibration.get("posture_amplitude")
    if amplitude is None:
        return
    if not isinstance(amplitude, dict):
        raise ConfigError("gait_calibration.posture_amplitude 는 매핑이어야 함")

    for name in ("stride_hz", "pitch_p95_deg", "pitch_max_deg", "roll_p95_deg", "roll_max_deg"):
        value = amplitude.get(name)
        if not _finite_number(value) or value <= 0:
            raise ConfigError(f"posture_amplitude.{name} 는 0 보다 큰 실측값이어야 함")

    # 최대가 p95 보다 작으면 둘 중 하나를 잘못 옮겨 적은 것이다.
    for axis in ("pitch", "roll"):
        if amplitude[f"{axis}_max_deg"] < amplitude[f"{axis}_p95_deg"]:
            raise ConfigError(f"posture_amplitude.{axis}_max_deg 가 p95 보다 작음")

    cycles = amplitude.get("cycles")
    if not isinstance(cycles, int) or isinstance(cycles, bool) or cycles < 30:
        raise ConfigError("posture_amplitude.cycles 는 30 이상 정수여야 함")

    # 폰으로 잰 것과 온보드 IMU 로 잰 것은 장착 위치·강성이 달라 값이 달라진다.
    # 어느 쪽인지 모르면 비교할 수 없으므로 값과 함께 남긴다.
    if amplitude.get("source") not in {"phone_imu", "onboard_imu"}:
        raise ConfigError("posture_amplitude.source 는 phone_imu 또는 onboard_imu 여야 함")
    if not isinstance(amplitude.get("measured_on"), str) or not amplitude["measured_on"]:
        raise ConfigError("posture_amplitude.measured_on 기록이 필요함")


def load_base_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    """개체 정보가 필요 없는 도구·테스트용 전역 설정 로더."""
    config = _read_mapping(path)
    validate_base_config(config)
    return config


def load_config(
    device_id: str | None,
    *,
    config_path: Path = DEFAULT_CONFIG,
    devices_dir: Path = DEFAULT_DEVICES_DIR,
) -> dict[str, Any]:
    """전역 설정에 ``<device>.yaml``과 선택적 로컬 덮어쓰기를 병합한다."""
    if not isinstance(device_id, str) or not device_id.strip():
        raise ConfigError("--device <unit-id> 를 지정해야 함")
    if Path(device_id).name != device_id:
        raise ConfigError("device_id 에 경로 문자를 사용할 수 없음")

    config = load_base_config(config_path)
    profile_path = devices_dir / f"{device_id}.yaml"
    config = _merge(config, _read_mapping(profile_path))

    local_path = devices_dir / f"{device_id}.local.yaml"
    if local_path.is_file():
        config = _merge(config, _read_mapping(local_path))

    validate_base_config(config)
    validate_device_config(config, device_id)
    return config
